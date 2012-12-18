
import os
import sys
from functools import wraps
from getpass import getuser
from contextlib import contextmanager

from fabric.api import env, cd, prefix, sudo as _sudo, run as _run, hide, task
from fabric.contrib.files import exists, upload_template
from fabric.colors import yellow, green, blue, red


################
# Config setup #
################

conf = {}
if sys.argv[0].split(os.sep)[-1] == "fab":
    # Ensure we import settings from the current dir
    try:
        conf = __import__("settings", globals(), locals(), [], 0).FABRIC
        try:
            conf["HOSTS"][0]
        except (KeyError, ValueError):
            raise ImportError
    except (ImportError, AttributeError):
        print "Aborting, no hosts defined."
        exit()

env.admin_pass = conf.get("ADMIN_PASS", None)
env.user = conf.get("SSH_USER", getuser())
env.password = conf.get("SSH_PASS", None)
env.key_filename = conf.get("SSH_KEY_PATH", None)
env.hosts = conf.get("HOSTS", [])

env.proj_name = conf.get("PROJECT_NAME", os.getcwd().split(os.sep)[-1])
env.proj_path = "/home/%s/%s" % (env.user, env.proj_name)
env.live_host = conf.get("LIVE_HOSTNAME", env.hosts[0] if env.hosts else None)
env.repo_url = conf.get("REPO_URL", None)
env.locale = conf.get("LOCALE", "en_US.UTF-8")


##################
# Template setup #
##################

# Each template gets uploaded at deploy time, only if their
# contents has changed, in which case, the reload command is
# also run.

templates = {
    "nginx": {
        "local_path": "deploy/nginx.conf",
        "remote_path": "/etc/nginx/sites-enabled/%(live_host)s.conf",
        "reload_command": "service nginx restart",
    },
    "supervisor": {
        "local_path": "deploy/supervisor.conf",
        "remote_path": "/etc/supervisor/conf.d/%(proj_name)s.conf",
        "reload_command": "supervisorctl reload",
    },
}


######################################
# Context for project #
######################################

@contextmanager
def project():
    """
    Runs commands within the project's directory.
    """
    with cd(env.proj_path):
        yield


###########################################
# Utils and wrappers for various commands #
###########################################

def _print(output):
    print
    print output
    print


def print_command(command):
    _print(blue("$ ", bold=True) +
           yellow(command, bold=True) +
           red(" ->", bold=True))


@task
def run(command, show=True):
    """
    Runs a shell comand on the remote server.
    """
    if show:
        print_command(command)
    with hide("running"):
        return _run(command)


@task
def sudo(command, show=True):
    """
    Runs a command as sudo.
    """
    if show:
        print_command(command)
    with hide("running"):
        return _sudo(command)


def log_call(func):
    @wraps(func)
    def logged(*args, **kawrgs):
        header = "-" * len(func.__name__)
        _print(green("\n".join([header, func.__name__, header]), bold=True))
        return func(*args, **kawrgs)
    return logged


def get_templates():
    """
    Returns each of the templates with env vars injected.
    """
    injected = {}
    for name, data in templates.items():
        injected[name] = dict([(k, v % env) for k, v in data.items()])
    return injected


def upload_template_and_reload(name):
    """
    Uploads a template only if it has changed, and if so, reload a
    related service.
    """
    template = get_templates()[name]
    local_path = template["local_path"]
    remote_path = template["remote_path"]
    reload_command = template.get("reload_command")
    owner = template.get("owner")
    mode = template.get("mode")
    remote_data = ""
    if exists(remote_path):
        with hide("stdout"):
            remote_data = sudo("cat %s" % remote_path, show=False)
    with open(local_path, "r") as f:
        local_data = f.read()
        if "%(db_pass)s" in local_data:
            env.db_pass = db_pass()
        local_data %= env
    clean = lambda s: s.replace("\n", "").replace("\r", "").strip()
    if clean(remote_data) == clean(local_data):
        return
    upload_template(local_path, remote_path, env, use_sudo=True, backup=False)
    if owner:
        sudo("chown %s %s" % (owner, remote_path))
    if mode:
        sudo("chmod %s %s" % (mode, remote_path))
    if reload_command:
        sudo(reload_command)


@task
def apt(packages):
    """
    Installs one or more system packages via apt.
    """
    return sudo("apt-get install -y -q " + packages)


#########################
# Install and configure #
#########################

@task
@log_call
def install():
    """
    Installs the base system for the entire server.
    """
    locale = "LC_ALL=%s" % env.locale
    with hide("stdout"):
        if locale not in sudo("cat /etc/default/locale"):
            sudo("update-locale %s" % locale)
            run("exit")
    sudo("apt-get update -y -q")
    apt("nginx supervisor git-core")
    return True


@task
@log_call
def create():
    """
    Pulls the project's repo from version control, adds system-level
    configs for the project.
    """
    run("git clone %s %s" % (env.repo_url, env.proj_path))
    return True


@task
@log_call
def remove():
    """
    Blow away the current project.
    """
    for template in get_templates().values():
        remote_path = template["remote_path"]
        if exists(remote_path):
            sudo("rm %s" % remote_path)


##############
# Deployment #
##############

@task
@log_call
def restart():
    """
    Restart nginx.
    """
    pid_path = "/var/run/nginx.pid"
    if exists(pid_path):
        sudo("kill -HUP `cat %s`" % pid_path)
    else:
        sudo("supervisorctl start %s:nginx" % env.proj_name)


@task
@log_call
def deploy():
    """
    Deploy latest version of the project.
    Check out the latest version of the project from version control.
    """
    for name in get_templates():
        upload_template_and_reload(name)
    with project():
        run("git rev-parse HEAD > last.commit")
        run("git pull origin master")
    restart()
    return True


@task
@log_call
def rollback():
    """
    Reverts project state to the last deploy.
    When a deploy is performed, the current state of the project is
    backed up. This includes the last commit checked out, the database,
    and all static files. Calling rollback will revert all of these to
    their state prior to the last deploy.
    """
    with project():
        run("git checkout `cat last.commit`")
    restart()


@task
@log_call
def all():
    """
    Installs everything required on a new system and deploy.
    From the base software, up to the deployed project.
    """
    install()
    if create():
        deploy()
