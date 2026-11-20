# localbox-dev

A small, per-repository rootless Podman dev box you can SSH into. Run it from inside any repo and you get a disposable Ubuntu container with your usual tools, while only that repo folder is mounted read-write on your Mac.

## What it is

- `devbox-base` — a Podman image based on `ubuntu:latest` with Rust CLI tools, `uv`, `pixi`, `bun`, `opencode`, Neovim + plugins, and more.
- `localbox` — a tiny command that creates, connects to, and deletes per-repo containers from that image.

Each box is named `<repo>-localbox` and mounts `$PWD` at `/home/dev/<repo>` inside the container. Nothing else from your Mac is visible.

## Requirements

- macOS with [Podman](https://podman.io/) installed and a working Podman machine.
- SSH key at `~/.ssh/id_ed25519.pub` (or set `LOCALBOX_PUBKEY`).
- For the TypeScript CLI: [Bun](https://bun.sh/).
- For the Python CLI: [uv](https://docs.astral.sh/uv/).

## Install

```sh
# TypeScript / Bun
bun install -g /path/to/this/repo

# Python / uv
uv tool install /path/to/this/repo/localbox-cli
```

## Build the image

Run this once, from the repo root:

```sh
podman build --build-arg UID=$(id -u) -t devbox-base .
```

`UID` must match your macOS user id, otherwise the mounted folder is read-only for the container user.

## Usage

From inside any project:

```sh
localbox create    # start a box for this repo
localbox connect   # ssh into it (starts it if stopped)
localbox display   # show the ssh command without changing anything
localbox delete    # remove the box; the host folder is kept
```

Once connected, you land in `/home/dev/<repo>` with zsh.

## SSH / Git

The generated SSH config forwards your macOS SSH agent, so git operations over SSH and `gh` via SSH work without copying keys into the image. GitHub's host keys are baked in, so the first connection is prompt-free.

## Why a stale-mount check

Podman bind mounts follow the folder's inode, not its path. If you move the repo aside and re-clone it at the same path, an existing box would still show the old folder. `localbox connect` and `localbox display` detect this and tell you to recreate the box.

## Image size

The final image is roughly 2 GB. The largest layers are the apt packages, the Python tools installed by `uv`, the Rust CLI binaries, and `opencode`.
