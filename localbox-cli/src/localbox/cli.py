import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

USAGE = """Usage: localbox <connect|create|display|delete>

Run from inside a repository. The box is named <repo>-localbox and mounts
the current directory at /home/dev/<repo>.

  create   start a new box from {image}
  connect  start (if stopped) and ssh into the box
  display  show the ssh command for this box (changes nothing)
  delete   remove the box; the host directory is kept

Environment:
  LOCALBOX_IMAGE   image to run (default: devbox-base)
  LOCALBOX_PUBKEY  public key to authorize (default: ~/.ssh/id_ed25519.pub)
"""

CONF_INCLUDE = "Include ~/.ssh/localbox.d/*.conf"


def die(msg: str) -> "None":
    print(f"localbox: {msg}", file=sys.stderr)
    sys.exit(1)


class Box:
    def __init__(self) -> None:
        self.image = os.environ.get("LOCALBOX_IMAGE", "devbox-base")
        self.pubkey = Path(
            os.environ.get("LOCALBOX_PUBKEY", Path.home() / ".ssh" / "id_ed25519.pub")
        )
        self.ssh_dir = Path.home() / ".ssh"
        self.conf_dir = self.ssh_dir / "localbox.d"
        self.cwd = Path.cwd()
        repo = re.sub(r"[^A-Za-z0-9._-]", "-", self.cwd.name)
        self.name = f"{repo}-localbox"
        self.mount_dir = f"/home/dev/{repo}"
        self.conf_path = self.conf_dir / f"{self.name}.conf"

    def podman(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        try:
            r = subprocess.run(["podman", *args], capture_output=True, text=True)
        except FileNotFoundError:
            die("podman not found on PATH")
        if check and r.returncode != 0:
            die(r.stderr.strip() or f"podman {' '.join(args)} failed")
        return r

    def exists(self) -> bool:
        return self.podman("container", "exists", self.name, check=False).returncode == 0

    def running(self) -> bool:
        out = self.podman("inspect", "-f", "{{.State.Running}}", self.name).stdout
        return out.strip() == "true"

    def host_port(self) -> str:
        out = self.podman("port", self.name, "22/tcp").stdout
        return out.splitlines()[0].rsplit(":", 1)[1].strip()

    def mount_inode(self) -> str:
        return str(os.stat(self.cwd).st_ino)

    def check_mount(self) -> None:
        # a bind mount follows the folder's inode, not its path: if the folder was
        # moved and re-created (e.g. re-cloned) the box keeps showing the old one
        fmt = '{{index .Config.Labels "localbox.inode"}}'
        recorded = self.podman("inspect", "-f", fmt, self.name).stdout.strip()
        if recorded and recorded != self.mount_inode():
            die(
                f"the folder at {self.cwd} was replaced after {self.name} was created,\n"
                f"so the box still mounts the old one (e.g. a moved or re-cloned repo).\n"
                f"recreate it: localbox delete && localbox create"
            )

    def ensure_include(self) -> None:
        self.ssh_dir.mkdir(mode=0o700, exist_ok=True)
        cfg = self.ssh_dir / "config"
        current = cfg.read_text() if cfg.exists() else ""
        if CONF_INCLUDE not in current:
            cfg.write_text(f"{CONF_INCLUDE}\n{current}")
            cfg.chmod(0o600)

    def write_ssh_conf(self, port: str) -> None:
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        self.conf_path.write_text(
            f"Host {self.name}\n"
            f"  HostName 127.0.0.1\n"
            f"  Port {port}\n"
            f"  User dev\n"
            f"  ForwardAgent yes\n"
            f"  AddKeysToAgent yes\n"
            f"  RequestTTY yes\n"
            f"  RemoteCommand cd {self.mount_dir} && exec zsh -l\n"
            f"  UserKnownHostsFile /dev/null\n"
            f"  StrictHostKeyChecking no\n"
            f"  LogLevel ERROR\n"
        )

    def wait_ssh(self) -> None:
        # RemoteCommand=none: ssh refuses a command-line command otherwise
        probe = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "RemoteCommand=none", "-o", "RequestTTY=no",
            self.name, "true",
        ]
        for _ in range(30):
            if subprocess.run(probe, capture_output=True).returncode == 0:
                return
            time.sleep(1)
        die(f"sshd in {self.name} did not become ready")

    def check_uid(self) -> None:
        fmt = '{{index .Config.Labels "localbox.uid"}}'
        r = self.podman("image", "inspect", "-f", fmt, self.image, check=False)
        if r.returncode != 0:
            die(f"image {self.image} not found (build it first)")
        built = r.stdout.strip()
        if built and built != str(os.getuid()):
            die(
                f"{self.image} was built for uid {built}, you are uid {os.getuid()}: "
                f"the mounted folder would be read-only.\n"
                f"rebuild with: podman build --build-arg UID=$(id -u) -t {self.image} ."
            )

    def create(self) -> None:
        if self.exists():
            die(f"{self.name} already exists (use: localbox connect)")
        if not self.pubkey.is_file():
            die(f"public key not found: {self.pubkey} (set LOCALBOX_PUBKEY)")

        self.check_uid()
        self.podman(
            "run", "-d",
            "--name", self.name,
            "--label", "localbox=true",
            "--label", f"localbox.inode={self.mount_inode()}",
            "-p", "127.0.0.1::22",
            "-e", f"SSH_PUBKEY={self.pubkey.read_text().strip()}",
            "-v", f"{self.cwd}:{self.mount_dir}",
            self.image,
        )
        self.ensure_include()
        self.write_ssh_conf(self.host_port())
        self.wait_ssh()
        print(f"created {self.name} (mounted {self.cwd} at {self.mount_dir})")
        print("connect with: localbox connect")

    def connect(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist (run: localbox create)")
        self.check_mount()
        if not self.running():
            self.podman("start", self.name)

        self.ensure_include()
        self.write_ssh_conf(self.host_port())
        self.wait_ssh()
        os.execvp("ssh", ["ssh", self.name])

    def display(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist (run: localbox create)")
        self.check_mount()
        print(f"box:     {self.name} ({'running' if self.running() else 'stopped'})")
        print(f"mounted: {self.cwd} -> {self.mount_dir}")
        if not self.running():
            print("ssh:     not available while stopped; `localbox connect` starts it")
            return

        port = self.host_port()
        remote = shlex.quote(f"cd {self.mount_dir} && exec zsh -l")
        print(f"ssh:     ssh {self.name}")
        print(
            "direct:  ssh -t -p {port} -o UserKnownHostsFile=/dev/null "
            "-o StrictHostKeyChecking=no -o LogLevel=ERROR dev@127.0.0.1 {remote}".format(
                port=port, remote=remote
            )
        )
        conf = self.conf_path.read_text() if self.conf_path.exists() else ""
        if f"Port {port}\n" not in conf:
            print("note:    ssh config is missing or has an old port; `localbox connect` refreshes it")

    def delete(self) -> None:
        if not self.exists():
            die(f"{self.name} does not exist")
        answer = input(f"Delete {self.name}? Files in {self.cwd} are kept. [y/N] ")
        if answer.strip().lower() != "y":
            die("aborted")

        self.podman("rm", "-f", self.name)
        self.conf_path.unlink(missing_ok=True)
        print(f"deleted {self.name} (files in {self.cwd} kept)")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("create", "connect", "display", "delete"):
        print(USAGE.format(image=os.environ.get("LOCALBOX_IMAGE", "devbox-base")))
        sys.exit(1)
    try:
        getattr(Box(), cmd)()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
