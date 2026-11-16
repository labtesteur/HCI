#!/usr/bin/env python3
"""CLI de gestion du cluster HCI KVM/Libvirt/Ceph.

A executer depuis CT-ANSIBLE en root.
Le script utilise SSH pour piloter les noeuds HCI et Ceph RBD pour les disques.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

NODES = {
    "HCI-1": "192.168.10.201",
    "HCI-2": "192.168.10.202",
    "HCI-3": "192.168.10.203",
}
SSH_KEY = os.environ.get("HCI_SSH_KEY", "/root/.ssh/id_ed25519_hci_migration")
CEPH_ADMIN = os.environ.get("HCI_CEPH_ADMIN", "HCI-1")
RBD_POOL = os.environ.get("HCI_RBD_POOL", "vm-pool")
LIBVIRT_POOL = os.environ.get("HCI_LIBVIRT_POOL", "ceph-vm-pool")
LIBVIRT_NETWORK = os.environ.get("HCI_LIBVIRT_NETWORK", "default")
CEPH_ID = os.environ.get("HCI_CEPH_ID", "libvirt")
CEPH_CONF = "/etc/ceph/ceph.conf"
CEPH_KEYRING = "/etc/ceph/ceph.client.libvirt.keyring"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

class HCIError(RuntimeError):
    pass


def run(cmd, check=True, capture=True):
    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "commande en erreur").strip()
        raise HCIError(f"{shlex.join(cmd)}\n{detail}")
    return result


def ssh(node, command, check=True):
    if node not in NODES:
        raise HCIError(f"Noeud inconnu: {node}")
    return run([
        "ssh", "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"root@{NODES[node]}",
        command,
    ], check=check)


def validate_name(name):
    if not NAME_RE.fullmatch(name):
        raise HCIError("Nom invalide. Utiliser 1 a 63 caracteres: lettres, chiffres, ., _, -")


def active_node(vm):
    found = []
    for node in NODES:
        result = ssh(node, f"virsh domstate {shlex.quote(vm)}", check=False)
        if result.returncode == 0:
            state = result.stdout.strip()
            found.append((node, state))
    if len(found) > 1:
        raise HCIError(f"VM {vm} definie sur plusieurs noeuds: {found}")
    return found[0] if found else (None, None)


def ceph_status():
    result = ssh(CEPH_ADMIN, "cephadm shell -- ceph status -f json")
    return json.loads(result.stdout)


def cmd_status(_args):
    data = ceph_status()
    health = data.get("health", {}).get("status", "UNKNOWN")
    mons = data.get("monmap", {}).get("num_mons", "?")
    osds = data.get("osdmap", {}).get("num_osds", "?")
    up = data.get("osdmap", {}).get("num_up_osds", "?")
    in_osds = data.get("osdmap", {}).get("num_in_osds", "?")
    print(f"Ceph: {health} | MON: {mons} | OSD: {osds} ({up} up / {in_osds} in)")
    for node in NODES:
        host = ssh(node, "hostname", check=False)
        pool = ssh(node, f"virsh pool-info {shlex.quote(LIBVIRT_POOL)}", check=False)
        net = ssh(node, f"virsh net-info {shlex.quote(LIBVIRT_NETWORK)}", check=False)
        pool_ok = pool.returncode == 0 and "running" in pool.stdout
        net_ok = net.returncode == 0 and "Active:         yes" in net.stdout
        print(f"{node}: SSH={'OK' if host.returncode == 0 else 'ERREUR'} | RBD={'OK' if pool_ok else 'ERREUR'} | Reseau={'OK' if net_ok else 'ERREUR'}")


def cmd_vm_list(_args):
    print(f"{'NOEUD':<8} {'NOM':<28} ETAT")
    print("-" * 52)
    for node in NODES:
        result = ssh(node, "virsh list --all --name", check=False)
        if result.returncode != 0:
            print(f"{node:<8} {'-':<28} ERREUR")
            continue
        for vm in filter(None, (x.strip() for x in result.stdout.splitlines())):
            state = ssh(node, f"virsh domstate {shlex.quote(vm)}").stdout.strip()
            print(f"{node:<8} {vm:<28} {state}")


def cmd_vm_info(args):
    validate_name(args.name)
    node, state = active_node(args.name)
    if not node:
        raise HCIError(f"VM introuvable: {args.name}")
    print(f"Noeud: {node}\nEtat: {state}")
    print(ssh(node, f"virsh dominfo {shlex.quote(args.name)}").stdout.strip())
    print("Interfaces:")
    print(ssh(node, f"virsh domiflist {shlex.quote(args.name)}").stdout.strip())


def cmd_vm_start(args):
    validate_name(args.name)
    node, state = active_node(args.name)
    if not node:
        raise HCIError(f"VM introuvable: {args.name}")
    if state == "running":
        print(f"{args.name} est deja active sur {node}")
        return
    ssh(node, f"virsh start {shlex.quote(args.name)}")
    print(f"{args.name} demarree sur {node}")


def cmd_vm_stop(args):
    validate_name(args.name)
    node, state = active_node(args.name)
    if not node:
        raise HCIError(f"VM introuvable: {args.name}")
    if state in ("shut off", "shutdown"):
        print(f"{args.name} est deja arretee sur {node}")
        return
    ssh(node, f"virsh shutdown {shlex.quote(args.name)}")
    print(f"Arret propre demande pour {args.name} sur {node}")


def cmd_vm_migrate(args):
    validate_name(args.name)
    if args.destination not in NODES:
        raise HCIError("Destination invalide")
    source, state = active_node(args.name)
    if not source:
        raise HCIError(f"VM introuvable: {args.name}")
    if source == args.destination:
        raise HCIError("La VM est deja sur le noeud de destination")
    if state != "running":
        raise HCIError("Cette version de la CLI migre uniquement les VM actives")
    if ceph_status().get("health", {}).get("status") != "HEALTH_OK" and not args.force:
        raise HCIError("Ceph n'est pas HEALTH_OK. Utiliser --force uniquement apres analyse")
    destination_ip = NODES[args.destination]
    remote_uri = f"qemu+ssh://root@{args.destination}/system"
    command = (
        f"virsh migrate --live --persistent --undefinesource "
        f"--migrateuri tcp://{destination_ip} "
        f"{shlex.quote(args.name)} {shlex.quote(remote_uri)}"
    )
    ssh(source, command)
    print(f"{args.name} migree de {source} vers {args.destination}")


def cmd_vm_delete(args):
    validate_name(args.name)
    node, state = active_node(args.name)
    if not node:
        raise HCIError(f"VM introuvable: {args.name}")
    if not args.yes:
        answer = input(f"Supprimer definitivement {args.name} et son RBD ? [taper OUI] ")
        if answer != "OUI":
            print("Annule")
            return
    if state == "running":
        if not args.force:
            raise HCIError("VM active. L'arreter ou utiliser --force")
        ssh(node, f"virsh destroy {shlex.quote(args.name)}")
    ssh(node, f"virsh undefine {shlex.quote(args.name)} --nvram", check=False)
    for current in NODES:
        ssh(current, f"virsh pool-refresh {shlex.quote(LIBVIRT_POOL)}", check=False)
        check = ssh(current, f"virsh dominfo {shlex.quote(args.name)}", check=False)
        if check.returncode == 0:
            raise HCIError(f"La VM est encore definie sur {current}. RBD conserve")
    ssh(CEPH_ADMIN, f"rbd --id {CEPH_ID} --keyring {CEPH_KEYRING} -p {RBD_POOL} rm {shlex.quote(args.name)}")
    print(f"{args.name} et son volume RBD ont ete supprimes")


def cmd_vm_create(args):
    validate_name(args.name)
    if args.node not in NODES:
        raise HCIError("Noeud invalide")
    existing, _ = active_node(args.name)
    if existing:
        raise HCIError(f"Une VM {args.name} existe deja sur {existing}")
    rbd_check = ssh(CEPH_ADMIN, f"rbd --id {CEPH_ID} --keyring {CEPH_KEYRING} -p {RBD_POOL} info {shlex.quote(args.name)}", check=False)
    if rbd_check.returncode == 0:
        raise HCIError(f"Le volume RBD {args.name} existe deja")
    image = os.path.abspath(args.image)
    if not os.path.isfile(image):
        raise HCIError(f"Image introuvable sur CT-ANSIBLE: {image}")
    remote_image = f"/var/lib/libvirt/images/{args.name}-base.qcow2"
    print(f"Copie de l'image vers {args.node}...")
    run(["scp", "-i", SSH_KEY, image, f"root@{NODES[args.node]}:{remote_image}"])
    try:
        ssh(args.node, f"virsh vol-create-as {shlex.quote(LIBVIRT_POOL)} {shlex.quote(args.name)} {args.disk}G")
        target = (
            f"rbd:{RBD_POOL}/{args.name}:id={CEPH_ID}:conf={CEPH_CONF}:keyring={CEPH_KEYRING}"
        )
        ssh(args.node, (
            f"qemu-img convert -p -n -f qcow2 -O raw {shlex.quote(remote_image)} "
            f"{shlex.quote(target)}"
        ))
        ssh(args.node, (
            f"virt-install --name {shlex.quote(args.name)} --memory {args.memory} "
            f"--vcpus {args.vcpus} --cpu host-model --import "
            f"--disk vol={LIBVIRT_POOL}/{args.name},bus=virtio "
            f"--network network={LIBVIRT_NETWORK},model=virtio "
            f"--osinfo debian13 --graphics none "
            f"--console pty,target_type=serial --noautoconsole"
        ))
    except Exception:
        print("Creation interrompue. Le volume peut devoir etre nettoye manuellement.", file=sys.stderr)
        raise
    finally:
        ssh(args.node, f"rm -f {shlex.quote(remote_image)}", check=False)
    print(f"VM {args.name} creee et demarree sur {args.node}")
    print("Note: cette V1 n'injecte pas encore Cloud-Init.")


def parser_build():
    parser = argparse.ArgumentParser(prog="hci", description="Gestion CLI du cluster HCI")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="Etat du cluster")
    p.set_defaults(func=cmd_status)
    vm = sub.add_parser("vm", help="Gestion des VM")
    vm_sub = vm.add_subparsers(dest="vm_command", required=True)
    p = vm_sub.add_parser("list"); p.set_defaults(func=cmd_vm_list)
    p = vm_sub.add_parser("info"); p.add_argument("name"); p.set_defaults(func=cmd_vm_info)
    p = vm_sub.add_parser("start"); p.add_argument("name"); p.set_defaults(func=cmd_vm_start)
    p = vm_sub.add_parser("stop"); p.add_argument("name"); p.set_defaults(func=cmd_vm_stop)
    p = vm_sub.add_parser("migrate")
    p.add_argument("name"); p.add_argument("destination", choices=NODES.keys()); p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_vm_migrate)
    p = vm_sub.add_parser("delete")
    p.add_argument("name"); p.add_argument("--yes", action="store_true"); p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_vm_delete)
    p = vm_sub.add_parser("create")
    p.add_argument("name")
    p.add_argument("--node", choices=NODES.keys(), default="HCI-1")
    p.add_argument("--image", required=True, help="QCOW2 local sur CT-ANSIBLE")
    p.add_argument("--disk", type=int, default=20, help="Taille RBD en Gio")
    p.add_argument("--memory", type=int, default=2048, help="RAM en Mio")
    p.add_argument("--vcpus", type=int, default=2)
    p.set_defaults(func=cmd_vm_create)
    return parser


def main():
    if os.geteuid() != 0:
        print("Executer cette CLI en root sur CT-ANSIBLE", file=sys.stderr)
        return 2
    try:
        args = parser_build().parse_args()
        args.func(args)
        return 0
    except HCIError as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu", file=sys.stderr)
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
