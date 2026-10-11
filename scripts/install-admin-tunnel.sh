#!/bin/bash
# Run as root with a public key file and the exact destination for this host.
set -euo pipefail
test "$(id -u)" = 0
key_file=${1:?Public key file required}
destination=${2:?Forwarding destination required}
case "$destination" in 10.77.0.1:22|10.77.0.1:18000) ;; *) exit 2 ;; esac
account=beresta-andrey
main=/etc/ssh/sshd_config
managed=/etc/ssh/beresta-andrey.conf
include="Include $managed"
test -f "$key_file" && test ! -L "$key_file"
test "$(wc -l < "$key_file")" = 1
test "$(ssh-keygen -lf "$key_file" | awk '{print $2}')" = SHA256:0vCwybtT7kDLG0b5BKbep4WvvEMA8ls5V6P49x/LDXU
test ! -e "$managed"
! grep -Fxq "$include" "$main"
! getent passwd "$account" >/dev/null
backup=$(mktemp /etc/ssh/sshd_config.before-beresta-andrey.XXXXXX)
cp -p "$main" "$backup"
baseline=$(mktemp)
/usr/sbin/sshd -T -C user=root,host=localhost,addr=10.77.0.2 > "$baseline"
rollback() {
    cp -p "$backup" "$main"
    rm -f -- "$managed" "$baseline"
}
trap rollback ERR
cat > "$managed" <<EOF
Match User $account
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    PermitOpen $destination
    PermitListen none
    AllowStreamLocalForwarding no
    MaxSessions 0
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PermitUserRC no
    PermitTunnel no
    GatewayPorts no
    ForceCommand /usr/bin/false
EOF
chmod 600 "$managed"
# Include at EOF: a Match block inside the initial *.d include could capture global directives.
printf '\n# Private administrator tunnel, separate from owner SSH access.\n%s\n' "$include" >> "$main"
/usr/sbin/sshd -t
after=$(mktemp)
/usr/sbin/sshd -T -C user=root,host=localhost,addr=10.77.0.2 > "$after"
cmp "$baseline" "$after"
useradd --create-home --user-group --shell /usr/sbin/nologin "$account"
home=/home/$account
test -d "$home" && test ! -L "$home"
install -d -o root -g root -m 755 "$home/.ssh"
printf 'restrict,port-forwarding,permitopen="%s" %s\n' "$destination" "$(cat "$key_file")" > "$home/.ssh/authorized_keys"
chown root:root "$home/.ssh/authorized_keys"
chmod 644 "$home/.ssh/authorized_keys"
# The key and its restrictions cannot be changed by the tunnel account.
chown root:root "$home"
chmod 755 "$home"
/usr/sbin/sshd -t
systemctl reload ssh
trap - ERR
rm -f -- "$baseline" "$after"
/usr/sbin/sshd -T -C user="$account",host=localhost,addr=10.77.0.2 | \
    awk '$1 ~ /^(allowtcpforwarding|permitopen|maxsessions|passwordauthentication|kbdinteractiveauthentication|permittty|allowagentforwarding|forcecommand)$/ {print}'
printf 'Tunnel-only user installed. Owner SSH configuration unchanged.\n'
