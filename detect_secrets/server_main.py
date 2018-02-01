#!/usr/bin/python
from __future__ import absolute_import
from __future__ import print_function

import codecs
import sys

import yaml

from detect_secrets.core.log import CustomLog
from detect_secrets.core.usage import ParserBuilder
from detect_secrets.hooks.pysensu_yelp import PySensuYelpHook
from detect_secrets.plugins import SensitivityValues
from detect_secrets.server import tracked_repo_factory
from detect_secrets.server.base_tracked_repo import DEFAULT_BASE_TMP_DIR
from detect_secrets.server.base_tracked_repo import OverrideLevel
from detect_secrets.server.repo_config import RepoConfig
from detect_secrets.server.s3_tracked_repo import S3Config


CustomLogObj = CustomLog()


def open_config_file(config_file):
    try:
        with codecs.open(config_file) as f:
            data = yaml.safe_load(f)

    except IOError:
        CustomLogObj.getLogger().error(
            'Unable to open config file: %s', config_file
        )

        raise

    return data


def add_repo(
        repo,
        plugin_sensitivity,
        is_local_repo=False,
        s3_config=None,
        repo_config=None,
):
    """Sets up an individual repo for tracking.

    :type repo: string
    :param repo: git URL or local path of repo to create TrackedRepo from.

    :type plugin_sensitivity: SensitivityValues
    :param plugin_sensitivity: namedtuple of configurable sensitivity values for plugins to be run

    :type is_local_repo: bool
    :param is_local_repo: true, if repo to be scanned exists locally (rather than solely managed
                          by this package)

    :type s3_config: S3Config
    :param s3_config: namedtuple of values to setup s3 connection. See `s3_tracked_repo` for more
                      details.

    :type repo_config: RepoConfig
    :param repo_config: namedtuple of values used to configure repositories.
    """
    args = {
        # We will set this value to HEAD upon first update
        'sha': '',
        'repo': repo,
        'plugin_sensitivity': plugin_sensitivity,
        's3_config': s3_config,
        'repo_config': repo_config,
    }

    repo = tracked_repo_factory(is_local_repo, bool(s3_config))(**args)

    # Clone the repo, if needed.
    repo.clone_and_pull_repo()

    # Make the last_commit_hash of repo point to HEAD
    repo.update()

    # Save the last_commit_hash, if we have nothing on file already.
    repo.save(OverrideLevel.NEVER)


def parse_sensitivity_values(args):
    """
    When configuring which plugins to run, the user is able to either
    specify a configuration file (with --config-file), or select individual
    values (eg. --base64-limit).

    This function handles parsing the values from these various places,
    and returning them as a SensitivityValues namedtuple.

    Order Precedence:
        1. Values specified in config file.
        2. Values specified inline. (eg. `--hex-limit 6`)
        3. Default values for CLI arguments (specified in ParserBuilder)

    :param args: parsed arguments from parse_args.
    :return: SensitivityValues
    """
    default_plugins = {}
    if args.config_file:
        data = open_config_file(args.config_file[0]).get('default', {})
        default_plugins = data.get('plugins', {})

    return SensitivityValues(
        base64_limit=default_plugins.get('Base64HighEntropyString') or
        args.base64_limit[0],
        hex_limit=default_plugins.get('HexHighEntropyString') or
        args.hex_limit[0],
        private_key_detector=default_plugins.get('PrivateKeyDetector') or
        not args.no_private_key_scan,
    )


def parse_s3_config(args):
    """
    :param args: parsed arguments from parse_args.
    :return: None if no s3_config_file specified.
    """
    if not args.s3_config_file:
        return None

    with codecs.open(args.s3_config_file[0]) as f:
        config = yaml.safe_load(f)

    try:
        return S3Config(**config)
    except TypeError:
        return None


def parse_repo_config(args):
    """
    :param args: parsed arguments from parse_args.
    :return: RepoConfig
    """
    default_repo_config = {}
    if args.config_file:
        default_repo_config = open_config_file(args.config_file[0]).get('default', {})

    return RepoConfig(
        default_repo_config.get('base_tmp_dir', DEFAULT_BASE_TMP_DIR),
        default_repo_config.get('baseline', '') or (args.baseline[0]),
        default_repo_config.get('exclude_regex', ''),
    )


def initialize_repos_from_repo_yaml(
    repo_yaml,
    plugin_sensitivity,
    repo_config,
    s3_config=None
):
    """For expected yaml file format, see `repos.yaml.sample`

    :type repo_yaml: string
    :param repo_yaml: filename of config file to read and parse

    :type plugin_sensitivity: SensitivityValues

    :type repo_config: RepoConfig

    :type s3_config: S3Config

    :return: list of TrackedRepos
    :raises: IOError
    """
    data = open_config_file(repo_yaml)

    output = []
    if data.get('tracked') is None:
        return output

    for entry in data['tracked']:
        sensitivity = plugin_sensitivity
        if entry.get('plugins'):
            # Merge plugin sensitivities
            plugin_dict = plugin_sensitivity._asdict()

            # Use SensitivityValues constructor to convert values
            entry_sensitivity = SensitivityValues(**entry['plugins'])
            plugin_dict.update(entry_sensitivity._asdict())

            sensitivity = SensitivityValues(**plugin_dict)

        entry['plugin_sensitivity'] = sensitivity

        config = repo_config
        if 'baseline_file' in entry:
            config = RepoConfig(
                base_tmp_dir=repo_config.base_tmp_dir,
                exclude_regex=repo_config.exclude_regex,
                baseline=entry['baseline_file'],
            )

        entry['repo_config'] = config

        if entry.get('s3_backed') and s3_config is None:
            CustomLogObj.getLogger().error(
                (
                    'Unable to load s3 config for %s. Make sure to specify '
                    '--s3-config-file for s3_backed repos!'
                ),
                entry.get('repo'),
            )
            continue
        entry['s3_config'] = s3_config

        # After setting up all arguments, create respective object.
        repo = tracked_repo_factory(
            entry.get('is_local_repo', False),
            entry.get('s3_backed', False),
        )
        output.append(repo(**entry))

    return output


def parse_args(argv):
    return ParserBuilder().add_initialize_server_argument() \
        .add_scan_repo_argument() \
        .add_config_file_argument() \
        .add_add_repo_argument() \
        .add_local_repo_flag() \
        .add_s3_config_file_argument() \
        .add_set_baseline_argument() \
        .parse_args(argv)


def main(argv=None):
    """
    Expected Usage:
      1. Initialize TrackedRepos from config.yaml, and save to crontab.
      2. Each cron command will run and scan git diff from previous commit saved, to now.
      3. If something is found, alert.

    :return: shell error code
    """
    if len(sys.argv) == 1:  # pragma: no cover
        sys.argv.append('-h')

    args = parse_args(argv)
    if args.verbose:    # pragma: no cover
        CustomLog.enableDebug(args.verbose)

    plugin_sensitivity = parse_sensitivity_values(args)
    repo_config = parse_repo_config(args)
    s3_config = parse_s3_config(args)

    if args.initialize:
        # initialize sets up the local file storage for tracking
        try:
            tracked_repos = initialize_repos_from_repo_yaml(
                args.initialize,
                plugin_sensitivity,
                repo_config,
                s3_config,
            )
        except IOError:
            # Error handled in initialize_repos_from_repo_yaml
            return 1

        cron_repos = [repo for repo in tracked_repos if repo.save()]
        if not cron_repos:
            return 0

        print('# detect-secrets scanner')
        for repo in cron_repos:
            print(repo.cron())

    elif args.add_repo:
        add_repo(
            args.add_repo[0],
            plugin_sensitivity,
            is_local_repo=args.local,
            s3_config=s3_config,
            repo_config=repo_config,
        )

    elif args.scan_repo:
        log = CustomLogObj.getLogger()

        repo_name = args.scan_repo[0]
        repo = tracked_repo_factory(args.local, bool(s3_config)) \
            .load_from_file(repo_name, repo_config, s3_config)
        if not repo:
            return 1

        secrets = repo.scan()

        if not secrets:
            return 1

        if len(secrets.data) > 0:
            log.error('SCAN COMPLETE - We found secrets in: %s', repo.name)
            alert = {
                'alert': 'Secrets found',
                'repo_name': repo.name,
                'secrets': secrets.json(),
                'authors': secrets.get_authors(repo),
            }
            log.error(alert)
            PySensuYelpHook('.pysensu.config.yaml').alert(secrets, repo.name)
        else:
            log.info('SCAN COMPLETE - STATUS: clean for %s', repo.name)

            # Save records, since the latest scan indicates that the most recent commit is clean
            repo.update()
            repo.save(OverrideLevel.ALWAYS)

    return 0


if __name__ == '__main__':
    sys.exit(main())
