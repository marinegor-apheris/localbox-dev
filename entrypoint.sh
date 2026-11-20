#!/bin/sh
set -eu

if [ -n "${SSH_PUBKEY:-}" ]; then
  install -d -m 700 -o dev -g dev /home/dev/.ssh
  printf '%s\n' "$SSH_PUBKEY" > /home/dev/.ssh/authorized_keys
  chown dev:dev /home/dev/.ssh/authorized_keys
  chmod 600 /home/dev/.ssh/authorized_keys
fi

exec /usr/sbin/sshd -D -e
