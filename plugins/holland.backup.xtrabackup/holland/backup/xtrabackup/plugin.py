"""
holland.mysql.xtrabackup
~~~~~~~~~~~~~~~~~~~~~~~

Backup plugin implementation to provide support for Percona XtraBackup.
"""

import logging
from distutils.version import LooseVersion
from os.path import join
from subprocess import PIPE, STDOUT, Popen, list2cmdline

from holland.backup.xtrabackup import util
from holland.core.backup import BackupError
from holland.core.util.path import directory_size
from holland.lib.compression import COMPRESSION_CONFIG_STRING, open_stream
from holland.lib.mysql import connect
from holland.lib.mysql.client.base import MYSQL_CLIENT_CONFIG_STRING
from holland.lib.mysql.option import build_mysql_config

LOG = logging.getLogger(__name__)

CONFIGSPEC = (
    """
[xtrabackup]
global-defaults     = string(default='/etc/my.cnf')
innobackupex        = string(default='innobackupex')
ibbackup            = string(default=None)
stream              = string(default=tar)
apply-logs          = boolean(default=yes)
slave-info          = boolean(default=no)
safe-slave-backup   = boolean(default=no)
no-lock             = boolean(default=no)
tmpdir              = string(default=None)
additional-options  = force_list(default=list())
pre-command         = string(default=None)
"""
    + MYSQL_CLIENT_CONFIG_STRING
    + COMPRESSION_CONFIG_STRING
)

CONFIGSPEC = CONFIGSPEC.splitlines()


class XtrabackupPlugin(object):
    """plugin for backuping database using xtrabackup"""

    #: control connection to mysql server
    mysql = None

    #: path to the my.cnf generated by this plugin
    defaults_path = None

    def __init__(self, name, config, target_directory, dry_run=False):
        self.name = name
        self.config = config
        self.config.validate_config(CONFIGSPEC)
        self.target_directory = target_directory
        self.dry_run = dry_run

        defaults_path = join(self.target_directory, "my.cnf")
        client_opts = self.config["mysql:client"]
        includes = [self.config["xtrabackup"]["global-defaults"]] + client_opts[
            "defaults-extra-file"
        ]
        util.generate_defaults_file(defaults_path, includes, client_opts)
        self.defaults_path = defaults_path

    def estimate_backup_size(self):
        """Return estimated backup size"""
        mysql_config = build_mysql_config(self.config["mysql:client"])
        client = connect(mysql_config["client"])
        try:
            datadir = client.show_variable("datadir")
            return directory_size(datadir)
        except OSError as exc:
            raise BackupError(
                "Failed to calculate directory size: [%d] %s" % (exc.errno, exc.strerror)
            )
        finally:
            client.close()

    def open_xb_logfile(self):
        """Open a file object to the log output for xtrabackup"""
        path = join(self.target_directory, "xtrabackup.log")
        try:
            return open(path, "a")
        except IOError as exc:
            raise BackupError("[%d] %s" % (exc.errno, exc.strerror))

    def open_xb_stdout(self):
        """Open the stdout output for a streaming xtrabackup run"""
        config = self.config["xtrabackup"]
        backup_directory = self.target_directory
        stream = util.determine_stream_method(config["stream"])
        if stream:
            if stream == "tar":
                archive_path = join(backup_directory, "backup.tar")
            elif stream == "xbstream":
                archive_path = join(backup_directory, "backup.xb")
            else:
                raise BackupError("Unknown stream method '%s'" % stream)
            try:
                return open_stream(archive_path, "w", **self.config["compression"])
            except OSError as exc:
                raise BackupError("Unable to create output file: %s" % exc)
        else:
            return open("/dev/null", "w")

    def dryrun(self, binary_xtrabackup):
        """Perform test backup"""
        xb_cfg = self.config["xtrabackup"]
        args = util.build_xb_args(
            xb_cfg, self.target_directory, self.defaults_path, binary_xtrabackup
        )
        LOG.info("* xtrabackup command: %s", list2cmdline(args))
        args = ["xtrabackup", "--defaults-file=" + self.defaults_path, "--help"]
        cmdline = list2cmdline(args)
        LOG.info("* Verifying generated config '%s'", self.defaults_path)
        LOG.debug("* Verifying via command: %s", cmdline)
        try:
            process = Popen(args, stdout=PIPE, stderr=STDOUT, close_fds=True)
        except OSError:
            raise BackupError("Failed to find xtrabackup binary")
        stdout = process.stdout.read()
        process.wait()
        # Note: xtrabackup --help will exit with 1 usually
        if process.returncode != 1:
            LOG.error("! %s failed. Output follows below.", cmdline)
            for line in stdout.splitlines():
                LOG.error("! %s", line)
            raise BackupError("%s exited with failure status [%d]" % (cmdline, process.returncode))

    def backup(self):
        """Perform Backup"""
        xtrabackup_version = util.xtrabackup_version()
        binary_xtrabackup = False
        if LooseVersion(xtrabackup_version) > LooseVersion("8.0.0"):
            LOG.debug("Use xtrabackup without innobackupex ")
            binary_xtrabackup = True

        if self.dry_run:
            self.dryrun(binary_xtrabackup)
            return

        xb_cfg = self.config["xtrabackup"]
        backup_directory = self.target_directory
        tmpdir = util.evaluate_tmpdir(xb_cfg["tmpdir"], backup_directory)
        # innobackupex --tmpdir does not affect xtrabackup
        util.add_xtrabackup_defaults(self.defaults_path, tmpdir=tmpdir)
        args = util.build_xb_args(xb_cfg, backup_directory, self.defaults_path, binary_xtrabackup)
        util.execute_pre_command(
            xb_cfg["pre-command"], backup_directory=backup_directory, backupdir=backup_directory
        )
        stderr = self.open_xb_logfile()
        try:
            stdout = self.open_xb_stdout()
            exc = None
            try:
                try:
                    util.run_xtrabackup(args, stdout, stderr)
                except Exception as exc:
                    LOG.info("!! %s", exc)
                    for line in open(join(self.target_directory, "xtrabackup.log"), "r"):
                        LOG.error("    ! %s", line.rstrip())
                    raise
            finally:
                try:
                    stdout.close()
                except IOError as ex:
                    LOG.error("Error when closing %s: %s", stdout.name, ex)
                    if exc is None:
                        raise
        finally:
            stderr.close()
        if xb_cfg["apply-logs"]:
            util.apply_xtrabackup_logfile(xb_cfg, backup_directory, binary_xtrabackup)
