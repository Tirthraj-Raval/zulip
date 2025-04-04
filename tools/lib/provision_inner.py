#!/usr/bin/env python3

############################## NOTE ################################
# This script is used to provision a development environment ONLY.
# For production, extend update-prod-static to generate new static
# assets, and puppet to install other software.
####################################################################

import argparse
import glob
import os
import pwd
import re
import shutil
import subprocess
import sys

ZULIP_PATH = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.append(ZULIP_PATH)
import pygments

from scripts.lib import clean_unused_caches
from scripts.lib.zulip_tools import (
    ENDC,
    OKBLUE,
    get_dev_uuid_var_path,
    get_tzdata_zi,
    is_digest_obsolete,
    run,
    run_as_root,
    write_new_digest,
)
from tools.setup.generate_bots_integrations_static_files import (
    generate_pythonapi_integrations_static_files,
    generate_zulip_bots_static_files,
)
from version import PROVISION_VERSION

VENV_PATH = os.path.join(ZULIP_PATH, ".venv")
UUID_VAR_PATH = get_dev_uuid_var_path()

with get_tzdata_zi() as f:
    line = f.readline()
    assert line.startswith("# version ")
    timezones_version = line.removeprefix("# version ")


def create_var_directories() -> None:
    # create var/coverage, var/log, etc.
    var_dir = os.path.join(ZULIP_PATH, "var")
    sub_dirs = [
        "coverage",
        "log",
        "node-coverage",
        "test_uploads",
        "uploads",
        "xunit-test-results",
    ]
    for sub_dir in sub_dirs:
        path = os.path.join(var_dir, sub_dir)
        os.makedirs(path, exist_ok=True)


def build_pygments_data_paths() -> list[str]:
    paths = [
        "tools/setup/build_pygments_data",
        "tools/setup/lang.json",
    ]
    return paths


def build_timezones_data_paths() -> list[str]:
    paths = [
        "tools/setup/build_timezone_values",
    ]
    return paths


def build_landing_page_images_paths() -> list[str]:
    paths = ["tools/setup/generate_landing_page_images.py"]
    paths += glob.glob("static/images/landing-page/hello/original/*")
    return paths


def compilemessages_paths() -> list[str]:
    paths = ["zerver/management/commands/compilemessages.py"]
    paths += glob.glob("locale/*/LC_MESSAGES/*.po")
    paths += glob.glob("locale/*/translations.json")
    return paths


def configure_rabbitmq_paths() -> list[str]:
    paths = [
        "scripts/setup/configure-rabbitmq",
    ]
    return paths


def is_wsl_instance() -> bool:
    if "WSL_DISTRO_NAME" in os.environ:
        return True

    with open("/proc/version") as file:
        content = file.read().lower()
        if "microsoft" in content:
            return True

    result = subprocess.run(["uname", "-r"], capture_output=True, text=True, check=True)
    if "microsoft" in result.stdout.lower():
        return True

    return False


def is_vagrant_or_digitalocean_instance() -> bool:
    user_id = os.getuid()
    user_name = pwd.getpwuid(user_id).pw_name
    return user_name in ["vagrant", "zulipdev"]


def setup_shell_profile(shell_profile: str) -> None:
    shell_profile_path = os.path.expanduser(shell_profile)
    if os.path.exists(shell_profile_path):
        with open(shell_profile_path) as f:
            code = f.read()
    else:
        code = ""

    # We want to activate the virtual environment for login shells only on virtualized systems.
    zulip_code = ""
    if is_vagrant_or_digitalocean_instance() or is_wsl_instance():
        zulip_code += (
            "if [ -L /srv/zulip-py3-venv ]; then\n"  # For development environment downgrades
            "source /srv/zulip-py3-venv/bin/activate\n"  # Not indented so old versions recognize and avoid re-adding this
            "else\n"
            f"source {os.path.join(VENV_PATH, 'bin', 'activate')}\n"
            "fi\n"
        )
    if os.path.exists("/srv/zulip"):
        zulip_code += "cd /srv/zulip\n"
    if zulip_code:
        zulip_code = f"\n# begin Zulip setup\n{zulip_code}# end Zulip setup\n"

    def patch_code(code: str) -> str:
        return re.sub(
            r"\n# begin Zulip setup\n(?s:.*)# end Zulip setup\n|(?:source /srv/zulip-py3-venv/bin/activate\n|cd /srv/zulip\n)+|\Z",
            lambda m: zulip_code,
            code,
            count=1,
        )

    new_code = patch_code(code)
    if new_code != code:
        assert patch_code(new_code) == new_code
        with open(f"{shell_profile_path}.new", "w") as f:
            f.write(new_code)
        os.rename(f"{shell_profile_path}.new", shell_profile_path)


def setup_bash_profile() -> None:
    """Select a bash profile file to add setup code to."""

    BASH_PROFILES = [
        os.path.expanduser(p) for p in ("~/.bash_profile", "~/.bash_login", "~/.profile")
    ]

    def clear_old_profile() -> None:
        # An earlier version of this script would output a fresh .bash_profile
        # even though a .profile existed in the image used. As a convenience to
        # existing developers (and, perhaps, future developers git-bisecting the
        # provisioning scripts), check for this situation, and blow away the
        # created .bash_profile if one is found.

        BASH_PROFILE = BASH_PROFILES[0]
        DOT_PROFILE = BASH_PROFILES[2]
        OLD_PROFILE_TEXT = "source /srv/zulip-py3-venv/bin/activate\ncd /srv/zulip\n"

        if os.path.exists(DOT_PROFILE):
            try:
                with open(BASH_PROFILE) as f:
                    profile_contents = f.read()
                if profile_contents == OLD_PROFILE_TEXT:
                    os.unlink(BASH_PROFILE)
            except FileNotFoundError:
                pass

    clear_old_profile()

    for candidate_profile in BASH_PROFILES:
        if os.path.exists(candidate_profile):
            setup_shell_profile(candidate_profile)
            break
    else:
        # no existing bash profile found; claim .bash_profile
        setup_shell_profile(BASH_PROFILES[0])


def need_to_run_build_pygments_data() -> bool:
    if not os.path.exists("web/generated/pygments_data.json"):
        return True

    return is_digest_obsolete(
        "build_pygments_data_hash",
        build_pygments_data_paths(),
        [pygments.__version__],
    )


def need_to_run_build_timezone_data() -> bool:
    if not os.path.exists("web/generated/timezones.json"):
        return True

    return is_digest_obsolete(
        "build_timezones_data_hash",
        build_timezones_data_paths(),
        [timezones_version],
    )


def need_to_regenerate_landing_page_images() -> bool:
    if not os.path.exists("static/images/landing-page/hello/generated"):
        return True

    return is_digest_obsolete(
        "landing_page_images_hash",
        build_landing_page_images_paths(),
    )


def need_to_run_compilemessages() -> bool:
    if not os.path.exists("locale/language_name_map.json"):
        # User may have cleaned their Git checkout.
        print("Need to run compilemessages due to missing language_name_map.json")
        return True

    return is_digest_obsolete(
        "last_compilemessages_hash",
        compilemessages_paths(),
    )


def need_to_run_configure_rabbitmq(settings_list: list[str]) -> bool:
    obsolete = is_digest_obsolete(
        "last_configure_rabbitmq_hash",
        configure_rabbitmq_paths(),
        settings_list,
    )

    if obsolete:
        return True

    try:
        from zerver.lib.queue import SimpleQueueClient

        SimpleQueueClient()
        return False
    except Exception:
        return True


def main(options: argparse.Namespace) -> int:
    setup_bash_profile()
    setup_shell_profile("~/.zprofile")

    # This needs to happen before anything that imports zproject.settings.
    run(["scripts/setup/generate_secrets.py", "--development"])

    create_var_directories()

    # The `build_emoji` script requires `emoji-datasource` package
    # which we install via npm; thus this step is after installing npm
    # packages.
    run(["tools/setup/emoji/build_emoji"])

    # copy over static files from the zulip_bots and integrations packages
    generate_zulip_bots_static_files()
    generate_pythonapi_integrations_static_files()

    if options.is_force or need_to_run_build_pygments_data():
        run(["tools/setup/build_pygments_data"])
        write_new_digest(
            "build_pygments_data_hash",
            build_pygments_data_paths(),
            [pygments.__version__],
        )
    else:
        print("No need to run `tools/setup/build_pygments_data`.")

    if options.is_force or need_to_run_build_timezone_data():
        run(["tools/setup/build_timezone_values"])
        write_new_digest(
            "build_timezones_data_hash",
            build_timezones_data_paths(),
            [timezones_version],
        )
    else:
        print("No need to run `tools/setup/build_timezone_values`.")

    if options.is_force or need_to_regenerate_landing_page_images():
        run(["tools/setup/generate_landing_page_images.py"])
        write_new_digest(
            "landing_page_images_hash",
            build_landing_page_images_paths(),
        )

    if not options.is_build_release_tarball_only:
        # The following block is skipped when we just need the development
        # environment to build a release tarball.

        # Need to set up Django before using template_status
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "zproject.settings")
        import django

        django.setup()

        from django.conf import settings

        from zerver.lib.test_fixtures import (
            DEV_DATABASE,
            TEST_DATABASE,
            destroy_leaked_test_databases,
        )

        assert settings.RABBITMQ_PASSWORD is not None
        if options.is_force or need_to_run_configure_rabbitmq([settings.RABBITMQ_PASSWORD]):
            run_as_root(["scripts/setup/configure-rabbitmq"])
            write_new_digest(
                "last_configure_rabbitmq_hash",
                configure_rabbitmq_paths(),
                [settings.RABBITMQ_PASSWORD],
            )
        else:
            print("No need to run `scripts/setup/configure-rabbitmq.")

        dev_template_db_status = DEV_DATABASE.template_status()
        if options.is_force or dev_template_db_status == "needs_rebuild":
            run(["tools/setup/postgresql-init-dev-db"])
            if options.skip_dev_db_build:
                # We don't need to build the manual development
                # database on continuous integration for running tests, so we can
                # just leave it as a template db and save a minute.
                #
                # Important: We don't write a digest as that would
                # incorrectly claim that we ran migrations.
                pass
            else:
                run(["tools/rebuild-dev-database"])
                DEV_DATABASE.write_new_db_digest()
        elif dev_template_db_status == "run_migrations":
            DEV_DATABASE.run_db_migrations()
        elif dev_template_db_status == "current":
            print("No need to regenerate the dev DB.")

        test_template_db_status = TEST_DATABASE.template_status()
        if options.is_force or test_template_db_status == "needs_rebuild":
            run(["tools/setup/postgresql-init-test-db"])
            run(["tools/rebuild-test-database"])
            TEST_DATABASE.write_new_db_digest()
        elif test_template_db_status == "run_migrations":
            TEST_DATABASE.run_db_migrations()
        elif test_template_db_status == "current":
            print("No need to regenerate the test DB.")

        if options.is_force or need_to_run_compilemessages():
            run(["./manage.py", "compilemessages", "--ignore=*"])
            write_new_digest(
                "last_compilemessages_hash",
                compilemessages_paths(),
            )
        else:
            print("No need to run `manage.py compilemessages`.")

        destroyed = destroy_leaked_test_databases()
        if destroyed:
            print(f"Dropped {destroyed} stale test databases!")

    clean_unused_caches.main(
        argparse.Namespace(
            threshold_days=6,
            # The defaults here should match parse_cache_script_args in zulip_tools.py
            dry_run=False,
            verbose=False,
            no_headings=True,
        )
    )

    # Keeping this cache file around can cause eslint to throw
    # random TypeErrors when new/updated dependencies are added
    if os.path.isfile(".eslintcache"):
        # Remove this block when
        # https://github.com/eslint/eslint/issues/11639 is fixed
        # upstream.
        os.remove(".eslintcache")

    # Clean up the root of the `var/` directory for various
    # testing-related files that we have migrated to
    # `var/<uuid>/test-backend`.
    print("Cleaning var/ directory files...")
    var_paths = glob.glob("var/test*")
    var_paths.append("var/bot_avatar")
    for path in var_paths:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except FileNotFoundError:
            pass

    version_file = os.path.join(UUID_VAR_PATH, "provision_version")
    print(f"writing to {version_file}\n")
    with open(version_file, "w") as f:
        f.write(".".join(map(str, PROVISION_VERSION)) + "\n")

    print()
    print(OKBLUE + "Zulip development environment setup succeeded!" + ENDC)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        dest="is_force",
        help="Ignore all provisioning optimizations.",
    )

    parser.add_argument(
        "--build-release-tarball-only",
        action="store_true",
        dest="is_build_release_tarball_only",
        help="Provision for test suite with production settings.",
    )

    parser.add_argument(
        "--skip-dev-db-build", action="store_true", help="Don't run migrations on dev database."
    )

    options = parser.parse_args()
    sys.exit(main(options))
