#!/usr/bin/env bun
// Run directly:  ./localbox.ts connect|create|delete   (needs bun on PATH)
// Or:            bun run localbox.ts connect|create|delete

import { basename, join } from "node:path";
import { homedir } from "node:os";
import { createInterface } from "node:readline/promises";
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";

const IMAGE = process.env.LOCALBOX_IMAGE ?? "devbox-base";
const PUBKEY = process.env.LOCALBOX_PUBKEY ?? join(homedir(), ".ssh", "id_ed25519.pub");
const SSH_DIR = join(homedir(), ".ssh");
const CONF_DIR = join(SSH_DIR, "localbox.d");
const CONF_INCLUDE = "Include ~/.ssh/localbox.d/*.conf";

const cwd = process.cwd();
const repo = basename(cwd).replace(/[^A-Za-z0-9._-]/g, "-");
const name = `${repo}-localbox`;
const mountDir = `/home/dev/${repo}`;
const confPath = join(CONF_DIR, `${name}.conf`);

const usage = `Usage: localbox <connect|create|display|delete>

Run from inside a repository. The box is named <repo>-localbox and mounts
the current directory at /home/dev/<repo>.

  create   start a new box from ${IMAGE}
  connect  start (if stopped) and ssh into the box
  display  show the ssh command for this box (changes nothing)
  delete   remove the box; the host directory is kept`;

function die(msg: string): never {
  console.error(`localbox: ${msg}`);
  process.exit(1);
}

async function podman(args: string[]): Promise<string> {
  const r = await Bun.$`podman ${args}`.nothrow().quiet();
  if (r.exitCode !== 0) {
    die(r.stderr.toString().trim() || `podman ${args.join(" ")} failed`);
  }
  return r.stdout.toString();
}

async function boxExists(): Promise<boolean> {
  const r = await Bun.$`podman container exists ${name}`.nothrow().quiet();
  return r.exitCode === 0;
}

async function isRunning(): Promise<boolean> {
  const out = await podman(["inspect", "-f", "{{.State.Running}}", name]);
  return out.trim() === "true";
}

async function hostPort(): Promise<string> {
  const out = await podman(["port", name, "22/tcp"]);
  const first = out.split("\n")[0] ?? "";
  return first.slice(first.lastIndexOf(":") + 1).trim();
}

const mountInode = () => String(statSync(cwd).ino);

// a bind mount follows the folder's inode, not its path: if the folder was
// moved and re-created (e.g. re-cloned) the box keeps showing the old one
async function checkMount() {
  const out = await podman(["inspect", "-f", '{{index .Config.Labels "localbox.inode"}}', name]);
  const recorded = out.trim();
  if (recorded && recorded !== mountInode()) {
    die(
      `the folder at ${cwd} was replaced after ${name} was created,\n` +
        `so the box still mounts the old one (e.g. a moved or re-cloned repo).\n` +
        `recreate it: localbox delete && localbox create`,
    );
  }
}

function ensureInclude() {
  mkdirSync(SSH_DIR, { recursive: true });
  const cfg = join(SSH_DIR, "config");
  const current = existsSync(cfg) ? readFileSync(cfg, "utf8") : "";
  if (!current.includes(CONF_INCLUDE)) {
    writeFileSync(cfg, `${CONF_INCLUDE}\n${current}`);
  }
}

function writeSshConf(port: string) {
  mkdirSync(CONF_DIR, { recursive: true });
  writeFileSync(
    confPath,
    `Host ${name}
  HostName 127.0.0.1
  Port ${port}
  User dev
  ForwardAgent yes
  AddKeysToAgent yes
  RequestTTY yes
  RemoteCommand cd ${mountDir} && exec zsh -l
  UserKnownHostsFile /dev/null
  StrictHostKeyChecking no
  LogLevel ERROR
`,
  );
}

async function waitSsh() {
  for (let i = 0; i < 30; i++) {
    // RemoteCommand=none: ssh refuses a command-line command otherwise
    const r = await Bun.$`ssh -o BatchMode=yes -o ConnectTimeout=3 -o RemoteCommand=none -o RequestTTY=no ${name} true`
      .nothrow()
      .quiet();
    if (r.exitCode === 0) return;
    await Bun.sleep(1000);
  }
  die(`sshd in ${name} did not become ready`);
}

async function create() {
  if (await boxExists()) die(`${name} already exists (use: localbox connect)`);
  if (!existsSync(PUBKEY)) die(`public key not found: ${PUBKEY} (set LOCALBOX_PUBKEY)`);

  const inspect = await Bun.$`podman image inspect -f ${'{{index .Config.Labels "localbox.uid"}}'} ${IMAGE}`
    .nothrow()
    .quiet();
  if (inspect.exitCode !== 0) die(`image ${IMAGE} not found (build it first)`);
  const built = inspect.stdout.toString().trim();
  const uid = String(process.getuid?.());
  if (built && built !== uid) {
    die(
      `${IMAGE} was built for uid ${built}, you are uid ${uid}: the mounted folder would be read-only.\n` +
        `rebuild with: podman build --build-arg UID=$(id -u) -t ${IMAGE} .`,
    );
  }

  const pubkey = readFileSync(PUBKEY, "utf8").trim();
  await podman([
    "run", "-d",
    "--name", name,
    "--label", "localbox=true",
    "--label", `localbox.inode=${mountInode()}`,
    "-p", "127.0.0.1::22",
    "-e", `SSH_PUBKEY=${pubkey}`,
    "-v", `${cwd}:${mountDir}`,
    IMAGE,
  ]);

  ensureInclude();
  writeSshConf(await hostPort());
  await waitSsh();
  console.log(`created ${name} (mounted ${cwd} at ${mountDir})`);
  console.log("connect with: localbox connect");
}

async function connect() {
  if (!(await boxExists())) die(`${name} does not exist (run: localbox create)`);
  await checkMount();
  if (!(await isRunning())) await podman(["start", name]);

  ensureInclude();
  writeSshConf(await hostPort());
  await waitSsh();

  const proc = Bun.spawn(["ssh", name], {
    stdin: "inherit",
    stdout: "inherit",
    stderr: "inherit",
  });
  process.exit(await proc.exited);
}

async function display() {
  if (!(await boxExists())) die(`${name} does not exist (run: localbox create)`);
  await checkMount();
  const running = await isRunning();
  console.log(`box:     ${name} (${running ? "running" : "stopped"})`);
  console.log(`mounted: ${cwd} -> ${mountDir}`);
  if (!running) {
    console.log("ssh:     not available while stopped; `localbox connect` starts it");
    return;
  }

  const port = await hostPort();
  const remote = `'${`cd ${mountDir} && exec zsh -l`.replaceAll("'", `'\\''`)}'`;
  console.log(`ssh:     ssh ${name}`);
  console.log(
    `direct:  ssh -t -p ${port} -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR dev@127.0.0.1 ${remote}`,
  );
  const conf = existsSync(confPath) ? readFileSync(confPath, "utf8") : "";
  if (!conf.includes(`Port ${port}\n`)) {
    console.log("note:    ssh config is missing or has an old port; `localbox connect` refreshes it");
  }
}

async function remove() {
  if (!(await boxExists())) die(`${name} does not exist`);

  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const answer = await rl.question(`Delete ${name}? Files in ${cwd} are kept. [y/N] `);
  rl.close();
  if (answer.trim().toLowerCase() !== "y") die("aborted");

  await podman(["rm", "-f", name]);
  rmSync(confPath, { force: true });
  console.log(`deleted ${name} (files in ${cwd} kept)`);
}

const commands: Record<string, () => Promise<void>> = {
  create,
  connect,
  display,
  delete: remove,
};

const cmd = process.argv[2] ?? "";
if (!(cmd in commands)) {
  console.log(usage);
  process.exit(1);
}
await commands[cmd]!();
