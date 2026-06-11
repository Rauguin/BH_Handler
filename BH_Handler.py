#!/usr/bin/env python3
"""
BH_Handler - gerenciador de sessoes para o BloodHound CE (pacote Kali).

Cada "sessao" e um par independente (banco PostgreSQL do BloodHound + grafo
Neo4j). Permite manter varios dominios/analises separados e atemporais,
trocando entre eles sem mexer no BloodHound na mao.

Uso:
  BH_Handler.py                 # sem argumento: mostra esta ajuda
  BH_Handler.py up              # sobe os servicos e RETOMA a sessao ativa
  BH_Handler.py new   <nome>    # cria uma sessao LIMPA (vazia) e troca pra ela
  BH_Handler.py clean           # zera os bancos VIVOS (sem nomear) -> scratch limpo
  BH_Handler.py use   <nome>    # troca para uma sessao salva (carrega os dados dela)
  BH_Handler.py list            # lista todas as sessoes (marca a ativa)
  BH_Handler.py save  [nome]    # salva o estado atual em disco (na ativa, ou num nome)
  BH_Handler.py del   <nome>    # remove uma sessao salva
  BH_Handler.py rename <a> <b>  # renomeia uma sessao
  BH_Handler.py stop            # para bhapi + neo4j
  BH_Handler.py status          # estado dos servicos e da sessao ativa

O script se auto-eleva com sudo (precisa de root para systemctl/neo4j/postgres).
Credenciais e caminhos sao lidos de /etc/bhapi/bhapi.json e /etc/neo4j/neo4j.conf,
entao funciona em qualquer Kali com o pacote `bloodhound` instalado.
"""

import json
import os
import pwd
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------- defaults (config)
# Estes valores sao sobrescritos por load_config() a partir dos arquivos do
# proprio BloodHound; servem so de fallback.
PG_DB       = "bloodhound"
PG_OWNER    = "_bloodhound"
PG_PASSWORD = "bloodhound"
PG_SUPER    = "postgres"                       # usuario de sistema do PostgreSQL
NEO4J_DATA  = Path("/etc/neo4j/data")
NEO4J_CONF  = Path("/etc/neo4j/neo4j.conf")
NEO4J_HTTP  = "http://localhost:7474/"
BHAPI_DIR   = "/usr/lib/bloodhound/bin"
BHAPI_BIN   = "./bhapi"
BHAPI_USER  = "_bloodhound"
BHAPI_PROC  = "bhapi"                           # nome do processo (comm) p/ pkill -x
BHAPI_CONF  = Path("/etc/bhapi/bhapi.json")
URL         = "http://127.0.0.1:8080"

# cores
R, B   = "\033[0m", "\033[1m"
RED    = "\033[1;31m"
GRN    = "\033[1;32m"
YLW    = "\033[1;33m"
CYN    = "\033[1;36m"


def info(msg): print(f"{GRN}[*]{R} {msg}")
def warn(msg): print(f"{YLW}[!]{R} {msg}")
def err(msg):  print(f"{RED}[x]{R} {msg}", file=sys.stderr)


# --------------------------------------------------------------- utilitarios
def out(cmd):
    """Roda e devolve stdout (str), engolindo erros."""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return p.stdout.strip()


def run(cmd, check=False, **kw):
    return subprocess.run(cmd, check=check, **kw)


def as_pg(args, **kw):
    """Roda um comando do PostgreSQL como o usuario de sistema do banco."""
    return subprocess.run(["runuser", "-u", PG_SUPER, "--", *args], **kw)


def real_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"


# ------------------------------------------------------------- carregamento
def load_config():
    """Le credenciais (bhapi.json) e caminhos (neo4j.conf) reais da maquina."""
    global PG_DB, PG_OWNER, PG_PASSWORD, NEO4J_DATA, BHAPI_DIR

    if BHAPI_CONF.exists():
        try:
            j = json.loads(BHAPI_CONF.read_text())
            db = j.get("database", {})
            PG_DB       = db.get("database", PG_DB)
            PG_OWNER    = db.get("username", PG_OWNER)
            PG_PASSWORD = db.get("secret", PG_PASSWORD)
        except (json.JSONDecodeError, OSError):
            pass

    NEO4J_DATA = detect_neo4j_data()

    for cand in (BHAPI_DIR, "/usr/lib/bloodhound/bin", "/usr/share/bloodhound/bin"):
        if Path(cand, "bhapi").exists():
            BHAPI_DIR = cand
            break


def detect_neo4j_data():
    """Descobre o diretorio de dados do Neo4j a partir do neo4j.conf."""
    data_val, home = None, Path("/usr/share/neo4j")
    if NEO4J_CONF.exists():
        for line in NEO4J_CONF.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = (x.strip() for x in line.split("=", 1))
            if k in ("server.directories.data", "dbms.directories.data"):
                data_val = v
            elif k in ("server.directories.neo4j_home", "dbms.directories.neo4j_home"):
                home = Path(v)
    if data_val:
        p = Path(data_val)
        return p if p.is_absolute() else home / p
    for c in (Path("/etc/neo4j/data"), Path("/var/lib/neo4j/data"), home / "data"):
        if c.exists():
            return c
    return Path("/etc/neo4j/data")


def check_prereqs():
    missing = [b for b in ("neo4j", "pg_lsclusters", "runuser", "psql") if not shutil.which(b)]
    if missing:
        err(f"Faltam binarios: {', '.join(missing)}. O pacote `bloodhound` esta instalado?")
        sys.exit(1)
    if not Path(BHAPI_DIR, "bhapi").exists():
        err(f"bhapi nao encontrado em {BHAPI_DIR}. Instale: sudo apt install bloodhound")
        sys.exit(1)


# ------------------------------------------------------------- area de dados
BASE = SESS_DIR = ACTIVE_F = LOG_F = None


def init_paths():
    global BASE, SESS_DIR, ACTIVE_F, LOG_F
    ru = real_user()
    try:
        home = Path(pwd.getpwnam(ru).pw_dir)
    except KeyError:
        home = Path.home()
    BASE     = home / ".bloodhound-sessions"
    SESS_DIR = BASE / "sessions"
    ACTIVE_F = BASE / "active"
    LOG_F    = BASE / "logs" / "bhapi.log"
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_F.parent.mkdir(parents=True, exist_ok=True)
    # devolve a posse ao usuario real (criamos como root)
    try:
        pw = pwd.getpwnam(ru)
        for p in (BASE, SESS_DIR, LOG_F.parent):
            os.chown(p, pw.pw_uid, pw.pw_gid)
    except (KeyError, PermissionError):
        pass


def session_path(name):
    return SESS_DIR / name


def valid_name(name):
    return name and "/" not in name and not name.startswith(".")


def active_session():
    if ACTIVE_F.exists():
        return ACTIVE_F.read_text().strip() or None
    return None


def set_active(name):
    if name is None:
        ACTIVE_F.unlink(missing_ok=True)
    else:
        ACTIVE_F.write_text(name)
    chown_real(BASE)


def chown_real(*paths):
    try:
        pw = pwd.getpwnam(real_user())
    except KeyError:
        return
    for p in paths:
        for root, dirs, files in os.walk(p):
            for d in (root, *(os.path.join(root, x) for x in dirs + files)):
                try:
                    os.chown(d, pw.pw_uid, pw.pw_gid)
                except (PermissionError, FileNotFoundError):
                    pass


# ---------------------------------------------------------------- PostgreSQL
def pg_cluster():
    """(versao, cluster, porta, status) do primeiro cluster PostgreSQL."""
    for line in out(["pg_lsclusters", "--no-header"]).splitlines():
        cols = line.split()
        if len(cols) >= 4:
            return cols[0], cols[1], cols[2], cols[3]
    return None


def ensure_postgres():
    cl = pg_cluster()
    if not cl:
        err("Nenhum cluster PostgreSQL encontrado.")
        sys.exit(1)
    ver, cluster, port, status = cl
    if status != "online":
        info(f"Subindo cluster PostgreSQL {ver}/{cluster} (porta {port})")
        run(["pg_ctlcluster", ver, cluster, "start"])
    for _ in range(30):
        if run(["pg_isready", "-p", port],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.5)
    warn("PostgreSQL nao respondeu a pg_isready a tempo (seguindo assim mesmo).")


def pg_kill_connections():
    as_pg(["psql", "-d", "postgres", "-c",
           f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
           f"WHERE datname='{PG_DB}' AND pid<>pg_backend_pid();"],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pg_set_password():
    as_pg(["psql", "-d", PG_DB, "-c",
           f"ALTER USER \"{PG_OWNER}\" WITH PASSWORD '{PG_PASSWORD}';"],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pg_recreate_empty():
    """Dropa e recria o banco vazio; o bhapi migra o schema no proximo boot."""
    pg_kill_connections()
    as_pg(["dropdb", "--if-exists", PG_DB], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    as_pg(["createdb", "-O", PG_OWNER, PG_DB], check=True)
    pg_set_password()


def pg_dump_to(dest: Path):
    with open(dest, "wb") as f:
        p = run(["runuser", "-u", PG_SUPER, "--", "pg_dump", "-Fc", PG_DB], stdout=f)
    if p.returncode != 0:
        raise RuntimeError("pg_dump falhou")


def pg_restore_from(src: Path):
    pg_kill_connections()
    as_pg(["dropdb", "--if-exists", PG_DB], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    as_pg(["createdb", "-O", PG_OWNER, PG_DB], check=True)
    with open(src, "rb") as f:
        run(["runuser", "-u", PG_SUPER, "--", "pg_restore", "--no-owner",
             f"--role={PG_OWNER}", "-d", PG_DB], stdin=f,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pg_set_password()


# ---------------------------------------------------------------------- Neo4j
def neo4j_running():
    p = run(["neo4j", "status"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return p.returncode == 0 and "is running" in (p.stdout or "")


def neo4j_start():
    if not neo4j_running():
        info("Subindo Neo4j")
        run(["neo4j", "start"])
    for _ in range(120):
        if run(["curl", "-s", NEO4J_HTTP],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.5)
    warn("Neo4j HTTP nao respondeu a tempo (seguindo assim mesmo).")


def neo4j_stop():
    if neo4j_running():
        info("Parando Neo4j")
        run(["neo4j", "stop"])
        for _ in range(60):
            if not neo4j_running():
                break
            time.sleep(0.5)


def neo4j_owner():
    st = NEO4J_DATA.stat()
    return st.st_uid, st.st_gid


def neo4j_snapshot_to(dest: Path):
    """tar.gz de TODO o diretorio de dados (Neo4j precisa estar parado)."""
    run(["tar", "czf", str(dest), "-C", str(NEO4J_DATA), "."], check=True)


def neo4j_restore_from(src: Path):
    uid, gid = neo4j_owner()
    for child in NEO4J_DATA.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    run(["tar", "xzf", str(src), "-C", str(NEO4J_DATA)], check=True)
    run(["chown", "-R", f"{uid}:{gid}", str(NEO4J_DATA)])


def neo4j_clean_graph():
    """Apaga so o grafo `neo4j`, preservando o db `system` (autenticacao)."""
    for sub in ("databases/neo4j", "transactions/neo4j"):
        p = NEO4J_DATA / sub
        if p.exists():
            shutil.rmtree(p)


# ---------------------------------------------------------------------- bhapi
def bhapi_running():
    return run(["pgrep", "-x", BHAPI_PROC], stdout=subprocess.DEVNULL).returncode == 0


def bhapi_start():
    if bhapi_running():
        info("bhapi ja esta rodando")
        return
    info("Subindo bhapi (pode demorar um pouco...)")
    logf = open(LOG_F, "ab")
    subprocess.Popen(["runuser", "-u", BHAPI_USER, "--", BHAPI_BIN],
                     cwd=BHAPI_DIR, stdout=logf, stderr=logf, start_new_session=True)
    for _ in range(180):
        if run(["curl", "-s", URL],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(1)
    warn(f"bhapi nao respondeu em {URL} a tempo. Veja o log: {LOG_F}")


def bhapi_stop():
    if bhapi_running():
        info("Parando bhapi")
        run(["pkill", "-x", BHAPI_PROC])
        for _ in range(30):
            if not bhapi_running():
                break
            time.sleep(0.5)


# -------------------------------------------------------------------- helpers
def stop_services():
    bhapi_stop()
    neo4j_stop()


def open_browser():
    ru = os.environ.get("SUDO_USER")
    cmd = ["runuser", "-u", ru, "--", "xdg-open", URL] if ru else ["xdg-open", URL]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_meta(name, description="", created=None):
    meta = {
        "name": name,
        "description": description,
        "created": created or datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    (session_path(name) / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def read_meta(name):
    f = session_path(name) / "meta.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            pass
    return {"name": name, "description": "", "created": "?", "updated": "?"}


def snapshot_to(name, reason=""):
    """Salva o estado VIVO atual na sessao `name` (Neo4j parado p/ consistencia)."""
    sp = session_path(name)
    sp.mkdir(parents=True, exist_ok=True)
    info(f"Salvando estado em '{name}' {reason}".rstrip())
    stop_services()
    ensure_postgres()
    pg_dump_to(sp / "pg.dump")
    neo4j_snapshot_to(sp / "neo4j_data.tar.gz")
    meta = read_meta(name)
    write_meta(name, meta.get("description", ""), meta.get("created"))
    chown_real(sp)


def snapshot_active(reason=""):
    """Persiste a sessao ativa (se houver uma nomeada) antes de algo destrutivo."""
    name = active_session()
    if name:
        snapshot_to(name, reason)
    else:
        warn("Sessao atual sem nome (scratch): dados nao salvos serao descartados.")


# -------------------------------------------------------------------- comandos
def cmd_up(args):
    name = active_session()
    ensure_postgres()
    neo4j_start()
    bhapi_start()
    print()
    label = f"{CYN}{name}{R}" if name else f"{YLW}(scratch, sem nome){R}"
    info(f"BloodHound no ar  |  sessao: {label}  |  {URL}")
    open_browser()


def cmd_new(args):
    if not args or not valid_name(args[0]):
        err("uso: BH_Handler.py new <nome> [descricao]")
        sys.exit(1)
    name, desc = args[0], " ".join(args[1:])
    if session_path(name).exists():
        err(f"Sessao '{name}' ja existe. Use 'use {name}' ou outro nome.")
        sys.exit(1)
    snapshot_active(reason="(antes de criar a nova)")
    info(f"Criando sessao LIMPA '{name}'")
    ensure_postgres()
    stop_services()
    pg_recreate_empty()
    neo4j_clean_graph()
    session_path(name).mkdir(parents=True, exist_ok=True)
    write_meta(name, desc)
    set_active(name)
    chown_real(session_path(name))
    neo4j_start()
    bhapi_start()
    print()
    info(f"Sessao '{name}' criada, vazia e ativa. {URL}")
    info("Login padrao: admin / admin")
    open_browser()


def cmd_clean(args):
    snapshot_active(reason="(antes de limpar)")
    info("Zerando os bancos (scratch limpo)")
    ensure_postgres()
    stop_services()
    pg_recreate_empty()
    neo4j_clean_graph()
    set_active(None)
    neo4j_start()
    bhapi_start()
    print()
    info(f"BloodHound limpo e no ar (scratch, sem nome). {URL}")
    info("Login padrao: admin / admin")
    open_browser()


def cmd_use(args):
    if not args:
        err("uso: BH_Handler.py use <nome>")
        sys.exit(1)
    name = args[0]
    if not session_path(name).exists():
        err(f"Sessao '{name}' nao existe. Veja 'list'.")
        sys.exit(1)
    if active_session() == name:
        info(f"'{name}' ja e a ativa. Subindo...")
        cmd_up([])
        return
    snapshot_active(reason="(antes de trocar)")
    info(f"Carregando sessao '{name}'")
    ensure_postgres()
    stop_services()
    sp = session_path(name)
    if (sp / "pg.dump").exists():
        pg_restore_from(sp / "pg.dump")
    else:
        warn("sessao sem dump PostgreSQL; criando banco vazio")
        pg_recreate_empty()
    if (sp / "neo4j_data.tar.gz").exists():
        neo4j_restore_from(sp / "neo4j_data.tar.gz")
    else:
        warn("sessao sem snapshot Neo4j; limpando grafo")
        neo4j_clean_graph()
    set_active(name)
    neo4j_start()
    bhapi_start()
    print()
    info(f"Sessao '{name}' ativa. {URL}")
    open_browser()


def cmd_save(args):
    name = args[0] if args else active_session()
    if not name:
        err("Sem sessao ativa. Use: BH_Handler.py save <nome>")
        sys.exit(1)
    if not valid_name(name):
        err(f"nome invalido: {name}")
        sys.exit(1)
    is_new = not session_path(name).exists()
    snapshot_to(name, reason="(checkpoint manual)")
    if args:
        set_active(name)
    info(f"{'Criada e salva' if is_new else 'Salva'} a sessao '{name}'. Reabrindo...")
    neo4j_start()
    bhapi_start()


def cmd_list(args):
    act = active_session()
    names = sorted(p.name for p in SESS_DIR.iterdir() if p.is_dir())
    if not names:
        info("Nenhuma sessao salva ainda. Crie com:  BH_Handler.py new <nome>")
        if act is None:
            info("Sessao viva atual: scratch (sem nome). Salve com:  save <nome>")
        return
    print(f"\n{B}Sessoes BloodHound{R}   (* = ativa)\n")
    print(f"   {'NOME':<18}{'CRIADA':<20}{'TAM':>7}  DESCRICAO")
    print(f"   {'-'*16:<18}{'-'*18:<20}{'-'*5:>7}  {'-'*18}")
    for n in names:
        meta = read_meta(n)
        size = (out(["du", "-sh", str(session_path(n))]).split("\t") or ["-"])[0] or "-"
        mark = f"{GRN}*{R}" if n == act else " "
        created = (meta.get("created") or "?")[:19].replace("T", " ")
        print(f" {mark} {n:<18}{created:<20}{size:>7}  {meta.get('description','')}")
    if act is None:
        print(f"\n   (sessao viva atual: scratch, sem nome)")
    print()


def cmd_del(args):
    if not args:
        err("uso: BH_Handler.py del <nome>")
        sys.exit(1)
    name = args[0]
    if not session_path(name).exists():
        err(f"Sessao '{name}' nao existe.")
        sys.exit(1)
    if active_session() == name:
        err(f"'{name}' e a sessao ATIVA. Troque com 'use <outra>' ou 'clean' antes de apagar.")
        sys.exit(1)
    shutil.rmtree(session_path(name))
    info(f"Sessao '{name}' removida.")


def cmd_rename(args):
    if len(args) < 2 or not valid_name(args[1]):
        err("uso: BH_Handler.py rename <antigo> <novo>")
        sys.exit(1)
    old, new = args[0], args[1]
    if not session_path(old).exists():
        err(f"Sessao '{old}' nao existe.")
        sys.exit(1)
    if session_path(new).exists():
        err(f"Sessao '{new}' ja existe.")
        sys.exit(1)
    session_path(old).rename(session_path(new))
    meta = read_meta(new)
    write_meta(new, meta.get("description", ""), meta.get("created"))
    if active_session() == old:
        set_active(new)
    info(f"'{old}' -> '{new}'")


def cmd_stop(args):
    stop_services()
    info("Servicos parados (PostgreSQL continua de pe para outros usos).")


def cmd_status(args):
    cl = pg_cluster()
    pg = f"{cl[0]}/{cl[1]} porta {cl[2]} [{cl[3]}]" if cl else "nao encontrado"
    act = active_session()
    print(f"\n{B}Status BloodHound{R}")
    print(f"  Sessao ativa : {CYN}{act}{R}" if act else f"  Sessao ativa : {YLW}(scratch, sem nome){R}")
    print(f"  PostgreSQL   : {pg}  (db={PG_DB})")
    print(f"  Neo4j        : {'rodando' if neo4j_running() else 'parado'}  (data={NEO4J_DATA})")
    print(f"  bhapi        : {'rodando' if bhapi_running() else 'parado'}")
    print(f"  URL          : {URL}")
    print(f"  Sessoes em   : {SESS_DIR}\n")


def cmd_help(args):
    print(__doc__)


COMMANDS = {
    "up": cmd_up, "start": cmd_up,
    "new": cmd_new,
    "clean": cmd_clean, "reset": cmd_clean, "fresh": cmd_clean,
    "use": cmd_use, "switch": cmd_use,
    "list": cmd_list, "ls": cmd_list,
    "save": cmd_save,
    "del": cmd_del, "rm": cmd_del, "delete": cmd_del,
    "rename": cmd_rename, "mv": cmd_rename,
    "stop": cmd_stop, "down": cmd_stop,
    "status": cmd_status, "st": cmd_status,
    "help": cmd_help, "-h": cmd_help, "--help": cmd_help,
}


def main():
    argv = sys.argv[1:]

    # Ajuda nao precisa de root: trata antes da auto-elevacao.
    if not argv or argv[0] in ("help", "-h", "--help"):
        cmd_help([])
        return

    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-E", sys.executable, os.path.abspath(__file__), *sys.argv[1:]])

    load_config()
    check_prereqs()
    init_paths()

    cmd = argv[0]
    handler = COMMANDS.get(cmd)
    if not handler:
        err(f"comando desconhecido: {cmd}\n")
        cmd_help([])
        sys.exit(1)
    handler(argv[1:])


if __name__ == "__main__":
    main()
