#!/usr/bin/env python3
import os, re, shlex, subprocess, sys

NODES={"HCI-1":"192.168.10.201","HCI-2":"192.168.10.202","HCI-3":"192.168.10.203"}
SSH_KEY="/root/.ssh/id_ed25519_hci_migration"
CEPH_ADMIN="HCI-1"
RBD_POOL="vm-pool"
LIBVIRT_POOL="ceph-vm-pool"
LIBVIRT_NETWORK="default"
CEPH_ID="libvirt"
CEPH_KEYRING="/etc/ceph/ceph.client.libvirt.keyring"
NAME_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

class HCIError(Exception): pass

def run(cmd,check=True):
    r=subprocess.run(cmd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if check and r.returncode:
        raise HCIError((r.stderr or r.stdout).strip())
    return r

def ssh(node,cmd,check=True):
    return run(["ssh","-i",SSH_KEY,"-o","BatchMode=yes","-o","ConnectTimeout=10",f"root@{NODES[node]}",cmd],check)

def pause(): input("\nEntrée pour revenir au menu...")
def cls(): os.system("clear")
def title(t): cls(); print("="*60); print(f" HCI MANAGER - {t}"); print("="*60)
def name_ok(n):
    if not NAME_RE.fullmatch(n): raise HCIError("Nom de VM invalide")

def locate(vm):
    found=[]
    for n in NODES:
        r=ssh(n,f"virsh domstate {shlex.quote(vm)}",False)
        if r.returncode==0: found.append((n,r.stdout.strip()))
    if len(found)>1: raise HCIError(f"VM définie sur plusieurs noeuds: {found}")
    return found[0] if found else (None,None)

def status():
    title("ÉTAT DU CLUSTER")
    s=ssh(CEPH_ADMIN,"cephadm shell -- ceph -s",False)
    print(s.stdout if s.returncode==0 else s.stderr)
    print("\nNoeuds:")
    for n in NODES:
        a=ssh(n,"hostname",False).returncode==0
        p=ssh(n,f"virsh pool-info {LIBVIRT_POOL}",False)
        net=ssh(n,f"virsh net-info {LIBVIRT_NETWORK}",False)
        print(f"  {n:<6} SSH={'OK' if a else 'KO':<3} RBD={'OK' if p.returncode==0 and 'running' in p.stdout else 'KO':<3} NET={'OK' if net.returncode==0 and 'Active:         yes' in net.stdout else 'KO'}")

def vm_list():
    title("LISTE DES VM")
    print(f"{'NOEUD':<8}{'VM':<28}ÉTAT")
    print("-"*50)
    for n in NODES:
        r=ssh(n,"virsh list --all --name",False)
        for vm in filter(None,(x.strip() for x in r.stdout.splitlines())):
            st=ssh(n,f"virsh domstate {shlex.quote(vm)}",False).stdout.strip()
            print(f"{n:<8}{vm:<28}{st}")

def vm_info():
    title("INFORMATIONS VM"); vm=input("Nom VM: ").strip(); name_ok(vm)
    n,st=locate(vm)
    if not n: raise HCIError("VM introuvable")
    print(f"\nNoeud: {n}\nÉtat: {st}\n")
    print(ssh(n,f"virsh dominfo {shlex.quote(vm)}").stdout)
    print(ssh(n,f"virsh domiflist {shlex.quote(vm)}",False).stdout)

def vm_start():
    title("DÉMARRER UNE VM"); vm=input("Nom VM: ").strip(); name_ok(vm); n,st=locate(vm)
    if not n: raise HCIError("VM introuvable")
    if st=="running": print("Déjà active."); return
    print(ssh(n,f"virsh start {shlex.quote(vm)}").stdout)

def vm_stop():
    title("ARRÊTER UNE VM"); vm=input("Nom VM: ").strip(); name_ok(vm); n,st=locate(vm)
    if not n: raise HCIError("VM introuvable")
    print(ssh(n,f"virsh shutdown {shlex.quote(vm)}").stdout)

def choose_node(prompt,exclude=None):
    opts=[n for n in NODES if n!=exclude]
    for i,n in enumerate(opts,1): print(f" {i}. {n} ({NODES[n]})")
    v=input(prompt).strip()
    try: return opts[int(v)-1]
    except: raise HCIError("Choix invalide")

def vm_migrate():
    title("MIGRATION LIVE")
    vm=input("Nom VM: ").strip(); name_ok(vm); src,st=locate(vm)
    if not src: raise HCIError("VM introuvable")
    if st!="running": raise HCIError("La VM doit être active pour ce test live")
    print(f"Source: {src}")
    dst=choose_node("Destination: ",src)
    if input(f"Migrer {vm} de {src} vers {dst} ? [o/N] ").lower()!="o": return
    uri=f"qemu+ssh://root@{dst}/system"
    c=f"virsh migrate --live --persistent --undefinesource --migrateuri tcp://{NODES[dst]} {shlex.quote(vm)} {shlex.quote(uri)}"
    ssh(src,c)
    print(f"Migration terminée: {src} -> {dst}")

def vm_delete():
    title("SUPPRESSION VM")
    vm=input("Nom VM: ").strip(); name_ok(vm); n,st=locate(vm)
    if not n: raise HCIError("VM introuvable")
    print(f"ATTENTION: suppression VM + volume Ceph RBD {RBD_POOL}/{vm}")
    if input('Tapez "SUPPRIMER" pour confirmer: ')!="SUPPRIMER": print("Annulé"); return
    if st=="running": ssh(n,f"virsh destroy {shlex.quote(vm)}")
    ssh(n,f"virsh undefine {shlex.quote(vm)} --nvram",False)
    for x in NODES:
        if ssh(x,f"virsh dominfo {shlex.quote(vm)}",False).returncode==0:
            raise HCIError(f"VM encore définie sur {x}, RBD conservé")
    ssh(CEPH_ADMIN,f"rbd --id {CEPH_ID} --keyring {CEPH_KEYRING} -p {RBD_POOL} rm {shlex.quote(vm)}")
    print("VM et volume supprimés.")

def vm_create():
    title("CRÉATION VM")
    print("V1: image Debian Cloud QCOW2, sans injection Cloud-Init automatique.\n")
    vm=input("Nom VM: ").strip(); name_ok(vm)
    node=choose_node("Noeud de création: ")
    image=input("Chemin QCOW2 sur CT-ANSIBLE: ").strip()
    if not os.path.isfile(image): raise HCIError("Image locale introuvable")
    try: disk=int(input("Disque Go [20]: ").strip() or "20"); mem=int(input("RAM Mo [2048]: ").strip() or "2048"); cpu=int(input("vCPU [2]: ").strip() or "2")
    except ValueError: raise HCIError("Valeur numérique invalide")
    if disk<2 or mem<256 or cpu<1: raise HCIError("Ressources invalides")
    if locate(vm)[0]: raise HCIError("Cette VM existe déjà")
    print(f"\nCréation {vm}: {cpu} vCPU, {mem} Mo, {disk} Go sur {node}")
    if input("Continuer ? [o/N] ").lower()!="o": return
    remote=f"/var/lib/libvirt/images/{vm}-base.qcow2"
    run(["scp","-i",SSH_KEY,image,f"root@{NODES[node]}:{remote}"])
    try:
        ssh(node,f"virsh vol-create-as {LIBVIRT_POOL} {shlex.quote(vm)} {disk}G")
        target=f"rbd:{RBD_POOL}/{vm}:id={CEPH_ID}:conf=/etc/ceph/ceph.conf:keyring={CEPH_KEYRING}"
        ssh(node,f"qemu-img convert -p -n -f qcow2 -O raw {shlex.quote(remote)} {shlex.quote(target)}")
        ssh(node,f"virt-install --name {shlex.quote(vm)} --memory {mem} --vcpus {cpu} --cpu host-model --import --disk vol={LIBVIRT_POOL}/{vm},bus=virtio --network network={LIBVIRT_NETWORK},model=virtio --osinfo debian13 --graphics none --console pty,target_type=serial --noautoconsole")
        print("VM créée et démarrée.")
    finally: ssh(node,f"rm -f {shlex.quote(remote)}",False)

def storage():
    title("STOCKAGE CEPH")
    print(ssh(CEPH_ADMIN,"cephadm shell -- ceph -s",False).stdout)
    print(ssh(CEPH_ADMIN,f"cephadm shell -- rbd ls {RBD_POOL}",False).stdout)

def nodes():
    title("NOEUDS HCI")
    for n in NODES:
        r=ssh(n,"uptime; echo; virsh list --all",False)
        print(f"\n--- {n} {NODES[n]} ---\n{r.stdout}")

def vm_menu():
    actions={"1":vm_list,"2":vm_create,"3":vm_info,"4":vm_start,"5":vm_stop,"6":vm_migrate,"7":vm_delete}
    while True:
        title("GESTION DES VM")
        print(" 1. Lister les VM\n 2. Créer une VM\n 3. Informations VM\n 4. Démarrer une VM\n 5. Arrêter une VM\n 6. Migrer une VM\n 7. Supprimer une VM\n 0. Retour")
        c=input("\nChoix: ").strip()
        if c=="0": return
        if c in actions:
            try: actions[c]()
            except HCIError as e: print(f"\nERREUR: {e}")
            pause()

def main():
    if os.geteuid()!=0: print("Exécuter en root sur CT-ANSIBLE"); return 1
    while True:
        title("MENU PRINCIPAL")
        print(" 1. État du cluster\n 2. Gestion des VM\n 3. État des noeuds\n 4. État du stockage Ceph\n 0. Quitter")
        c=input("\nChoix: ").strip()
        if c=="0": print("Au revoir."); return 0
        try:
            if c=="1": status(); pause()
            elif c=="2": vm_menu()
            elif c=="3": nodes(); pause()
            elif c=="4": storage(); pause()
        except HCIError as e: print(f"\nERREUR: {e}"); pause()

if __name__=="__main__": raise SystemExit(main())
