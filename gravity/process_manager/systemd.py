"""
"""
import errno
import os
import shlex
import subprocess
from glob import glob
from functools import partial

import gravity.io
from gravity.process_manager import BaseProcessManager
from gravity.settings import ProcessManager
from gravity.state import GracefulMethod

SYSTEMD_SERVICE_TEMPLATE = """;
; This file is maintained by Gravity - CHANGES WILL BE OVERWRITTEN
;

[Unit]
Description={systemd_description}
After=network.target
After=time-sync.target
PartOf={systemd_target}

[Service]
UMask={galaxy_umask}
Type=simple
{systemd_user_group}
WorkingDirectory={galaxy_root}
TimeoutStartSec={settings[start_timeout]}
TimeoutStopSec={settings[stop_timeout]}
ExecStart={command}
{systemd_exec_reload}
{environment}
{systemd_memory_limit}
Restart=always

MemoryAccounting=yes
CPUAccounting=yes
BlockIOAccounting=yes

[Install]
WantedBy=multi-user.target
"""

SYSTEMD_TARGET_TEMPLATE = """;
; This file is maintained by Gravity - CHANGES WILL BE OVERWRITTEN
;

[Unit]
Description={systemd_description}
After=network.target
After=time-sync.target
Wants={systemd_target_wants}

[Install]
WantedBy=multi-user.target
"""


class SystemdService:
    # converts between different formats
    def __init__(self, config, service, use_instance_name):
        self.config = config
        self.service = service
        self._use_instance_name = use_instance_name

        if use_instance_name:
            prefix_instance_name = f"-{config.instance_name}"
            description_instance_name = f" {config.instance_name}"
        else:
            prefix_instance_name = ""
            description_instance_name = ""

        if self.service.count > 1:
            description_process = " (process %i)"
        else:
            description_process = ""

        self.unit_prefix = f"{service.config_type}{prefix_instance_name}-{service.service_name}"
        self.description = f"{config.config_type.capitalize()}{description_instance_name} {service.service_name}{description_process}"

    @property
    def unit_file_name(self):
        instance_count = self.service.count
        if instance_count > 1:
            return f"{self.unit_prefix}@.service"
        else:
            return f"{self.unit_prefix}.service"

    @property
    def unit_names(self):
        """The representation when performing commands, after instance expansion"""
        instance_count = self.service.count
        if instance_count > 1:
            unit_names = [f"{self.unit_prefix}@{i}.service" for i in range(0, instance_count)]
        else:
            unit_names = [f"{self.unit_prefix}.service"]
        return unit_names


class SystemdProcessManager(BaseProcessManager):

    name = ProcessManager.systemd

    def __init__(self, foreground=False, **kwargs):
        super(SystemdProcessManager, self).__init__(**kwargs)
        self.user_mode = not self.config_manager.is_root

    @property
    def __systemd_unit_dir(self):
        unit_path = os.environ.get("GRAVITY_SYSTEMD_UNIT_PATH")
        if not unit_path:
            unit_path = "/etc/systemd/system" if not self.user_mode else os.path.expanduser("~/.config/systemd/user")
        return unit_path

    def __systemctl(self, *args, ignore_rc=None, capture=False, **kwargs):
        args = list(args)
        call = subprocess.check_call
        extra_args = os.environ.get("GRAVITY_SYSTEMCTL_EXTRA_ARGS")
        if extra_args:
            args = shlex.split(extra_args) + args
        if self.user_mode:
            args = ["--user"] + args
        gravity.io.debug("Calling systemctl with args: %s", args)
        if capture:
            call = subprocess.check_output
        try:
            return call(["systemctl"] + args, text=True)
        except subprocess.CalledProcessError as exc:
            if ignore_rc is None or exc.returncode not in ignore_rc:
                raise

    def __journalctl(self, *args, **kwargs):
        args = list(args)
        if self.user_mode:
            args = ["--user"] + args
        gravity.io.debug("Calling journalctl with args: %s", args)
        subprocess.check_call(["journalctl"] + args)

    def _service_default_path(self):
        environ = self.__systemctl("show-environment", capture=True)
        for line in environ.splitlines():
            if line.startswith("PATH="):
                return line.split("=", 1)[1]

    def _service_environment_formatter(self, environment, format_vars):
        return "\n".join("Environment={}={}".format(k, shlex.quote(v.format(**format_vars))) for k, v in environment.items())

    def terminate(self):
        # this is used to stop a foreground supervisord in the supervisor PM, so it is a no-op here
        pass

    def __target_unit_name(self, config):
        instance_name = f"-{config.instance_name}" if self._use_instance_name else ""
        return f"{config.config_type}{instance_name}.target"

    def __update_service(self, config, service, systemd_service: SystemdService):
        # under supervisor we expect that gravity is installed in the galaxy venv and the venv is active when gravity
        # runs, but under systemd this is not the case. we do assume $VIRTUAL_ENV is the galaxy venv if running as an
        # unprivileged user, though.
        virtualenv_dir = config.virtualenv
        environ_virtual_env = os.environ.get("VIRTUAL_ENV")
        if not virtualenv_dir and self.user_mode and environ_virtual_env:
            gravity.io.warn(f"Assuming Galaxy virtualenv is value of $VIRTUAL_ENV: {environ_virtual_env}")
            gravity.io.warn("Set `virtualenv` in Gravity configuration to override")
            virtualenv_dir = environ_virtual_env
        elif not virtualenv_dir:
            gravity.io.exception("The `virtualenv` Gravity config option must be set when using the systemd process manager")

        memory_limit = service.settings.get("memory_limit") or config.memory_limit
        if memory_limit:
            memory_limit = f"MemoryLimit={memory_limit}G"

        exec_reload = None
        if service.graceful_method == GracefulMethod.SIGHUP:
            exec_reload = "ExecReload=/bin/kill -HUP $MAINPID"

        # systemd-specific format vars
        systemd_format_vars = {
            "virtualenv_bin": f'{os.path.join(virtualenv_dir, "bin")}{os.path.sep}' if virtualenv_dir else "",
            "instance_number": "%i",
            "systemd_user_group": "",
            "systemd_exec_reload": exec_reload or "",
            "systemd_memory_limit": memory_limit or "",
            "systemd_description": systemd_service.description,
            "systemd_target": self.__target_unit_name(config),
        }
        if not self.user_mode:
            systemd_format_vars["systemd_user_group"] = f"User={config.galaxy_user}"
            if config.galaxy_group is not None:
                systemd_format_vars["systemd_user_group"] += f"\nGroup={config.galaxy_group}"

        format_vars = self._service_format_vars(config, service, systemd_format_vars)

        if not format_vars["command"].startswith("/"):
            format_vars["command"] = f"{format_vars['virtualenv_bin']}{format_vars['command']}"

        unit_file = systemd_service.unit_file_name
        conf = os.path.join(self.__systemd_unit_dir, unit_file)
        template = SYSTEMD_SERVICE_TEMPLATE
        contents = template.format(**format_vars)
        self._update_file(conf, contents, unit_file, "systemd unit")

        return conf

    def _process_config(self, config, clean=False, **kwargs):
        """ """
        intended_configs = set()

        try:
            os.makedirs(self.__systemd_unit_dir)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

        service_units = []
        for service in config.services:
            systemd_service = SystemdService(config, service, self._use_instance_name)
            if clean:
                intended_configs.add(os.path.join(self.__systemd_unit_dir, systemd_service.unit_file_name))
            else:
                conf = self.__update_service(config, service, systemd_service)
                intended_configs.add(conf)
                service_units.extend(systemd_service.unit_names)

        # create systemd target
        target_unit_name = self.__target_unit_name(config)
        target_conf = os.path.join(self.__systemd_unit_dir, target_unit_name)
        format_vars = {
            "systemd_description": config.config_type.capitalize(),
            "systemd_target_wants": " ".join(service_units),
        }
        if self._use_instance_name:
            format_vars["systemd_description"] += f" {config.instance_name}"
        contents = SYSTEMD_TARGET_TEMPLATE.format(**format_vars)
        updated = False
        if not clean:
            updated = self._update_file(target_conf, contents, target_unit_name, "systemd unit")
        intended_configs.add(target_conf)
        if updated:
            self.__systemctl("enable", target_conf)

        return intended_configs

    def _process_configs(self, configs, clean):
        intended_configs = set()

        for config in configs:
            intended_configs = intended_configs | self._process_config(config, clean=clean)

        # the unit dir might not exist if $GRAVITY_SYSTEMD_UNIT_PATH is set (e.g. for tests), but this is fine if there
        # are no intended configs
        if not intended_configs and not os.path.exists(self.__systemd_unit_dir):
            return

        # FIXME: should use config_type, but that's per-service
        _present_configs = filter(
            lambda f: (f.startswith("galaxy-") and (f.endswith(".service") or f.endswith(".target")) or f == "galaxy.target"),
            os.listdir(self.__systemd_unit_dir))
        present_configs = set([os.path.join(self.__systemd_unit_dir, f) for f in _present_configs])

        # if cleaning, then intended configs are actually *unintended* configs
        if clean:
            unintended_configs = present_configs & intended_configs
        else:
            unintended_configs = present_configs - intended_configs

        for file in unintended_configs:
            unit_name = os.path.basename(file)
            self.__systemctl("disable", "--now", unit_name)
            gravity.io.info("Removing systemd config %s", file)
            os.unlink(file)
            self._service_changes = True

    def __unit_names(self, configs, service_names, use_target=True, include_services=False):
        unit_names = []
        for config in configs:
            services = config.services
            if not service_names and use_target:
                unit_names.append(self.__target_unit_name(config))
                if not include_services:
                    services = []
            elif service_names:
                services = [s for s in config.services if s.service_name in service_names]
            systemd_services = [SystemdService(config, s, self._use_instance_name) for s in services]
            for systemd_service in systemd_services:
                unit_names.extend(systemd_service.unit_names)
        return unit_names

    def follow(self, configs=None, service_names=None, quiet=False):
        """ """
        unit_names = self.__unit_names(configs, service_names, use_target=False)
        u_args = [i for sl in list(zip(["-u"] * len(unit_names), unit_names)) for i in sl]
        self.__journalctl("-f", *u_args)

    def start(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("start", *unit_names)

    def stop(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("stop", *unit_names)

    def restart(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names)
        self.__systemctl("restart", *unit_names)

    def __graceful_service(self, config, service, service_names):
        systemd_service = SystemdService(config, service, self._use_instance_name)
        if service.graceful_method == GracefulMethod.ROLLING:
            restart_callbacks = list(partial(self.__systemctl, "reload-or-restart", u) for u in systemd_service.unit_names)
            service.rolling_restart(restart_callbacks)
        else:
            self.__systemctl("reload-or-restart", *systemd_service.unit_names)
            gravity.io.info(f"Restarted: {', '.join(systemd_service.unit_names)}")

    def graceful(self, configs=None, service_names=None):
        """ """
        # reload-or-restart on a target does a restart on its services, so we use the services directly
        for config in configs:
            if service_names:
                services = [s for s in config.services if s.service_name in service_names]
            else:
                services = config.services
            for service in services:
                self.__graceful_service(config, service, service_names)

    def status(self, configs=None, service_names=None):
        """ """
        unit_names = self.__unit_names(configs, service_names, include_services=True)
        try:
            self.__systemctl("status", "--lines=0", *unit_names, ignore_rc=(3,))
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 4:
                gravity.io.error("Some expected systemd units were not found, did you forget to run `galaxyctl update`?")
            else:
                raise

    def update(self, configs=None, force=False, clean=False):
        """ """
        if force:
            for config in configs:
                units = (glob(os.path.join(self.__systemd_unit_dir, f"{config.config_type}-*.service")) +
                         glob(os.path.join(self.__systemd_unit_dir, f"{config.config_type}-*.target")) +
                         glob(os.path.join(self.__systemd_unit_dir, f"{config.config_type}.target")))
                if units:
                    newline = '\n'
                    gravity.io.info(f"Removing systemd units due to --force option:{newline}{newline.join(units)}")
                    [self.__systemctl("disable", os.path.basename(u)) for u in units]
                    list(map(os.unlink, units))
                    self._service_changes = True
        self._process_configs(configs, clean)
        if self._service_changes:
            self.__systemctl("daemon-reload")
        else:
            gravity.io.debug("No service changes, daemon-reload not performed")

    def shutdown(self):
        """ """
        if self._use_instance_name:
            configs = self.config_manager.get_configs(process_manager=self.name)
            self.__systemctl("stop", *[f"galaxy-{c.instance_name}.target" for c in configs])
        else:
            self.__systemctl("stop", "galaxy.target")

    def pm(self, *args):
        """ """
        self.__systemctl(*args)
