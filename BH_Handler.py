#!/usr/bin/env python3
"""
BH_Handler - gerenciador de sessoes para o BloodHound CE (pacote Kali ou Docker Compose).

Cada "sessao" e um par independente (banco PostgreSQL do BloodHound + grafo
Neo4j). Permite manter varios dominios/analises separados e atemporais,
trocando entre eles sem mexer no BloodHound na mao.

Uso:
  BH_Handler.py [--docker|--local] [--compose-file PATH] <comando> [args]

Comandos:
  up                    # sobe os servicos e RETOMA a sessao ativa
  new   <nome> [desc]   # cria uma sessao LIMPA (vazia) e troca pra ela
  clean                 # zera os bancos VIVOS (sem nomear) -> scratch limpo
  use   <nome>          # troca para uma sessao salva (carrega os dados dela)
  list                  # lista todas as sessoes (marca a ativa)
  save  [nome]          # salva o estado atual em disco (na ativa, ou num nome)
  del   <nome>          # remove uma sessao salva
  rename <a> <b>        # renomeia uma sessao
  stop                  # para os servicos do BloodHound
  status                # estado do backend, dos servicos e da sessao ativa

Backends:
  --local               pacote Kali (`apt install bloodhound`): systemd/neo4j/bhapi
                        locais. Auto-eleva com sudo. Le /etc/bhapi/bhapi.json e
                        /etc/neo4j/neo4j.conf.
  --docker              docker-compose oficial da SpecterOps (servicos app-db,
                        graph-db, bloodhound). Nao precisa de root, so acesso ao
                        daemon Docker (grupo `docker`).
  --compose-file PATH   compose do BloodHound (implica --docker).

Selecao do backend (primeiro que existir):
  1. flag --docker / --local / --compose-file
  2. variavel BH_BACKEND=docker|local (e BH_COMPOSE_FILE=PATH)
  3. ~/.bloodhound-sessions/config.json (gravado quando voce passa uma flag)
  4. auto-deteccao: compose do BloodHound encontrado -> docker; senao -> local

As sessoes tem o mesmo formato nos dois backends (pg.dump + neo4j_data.tar.gz).
"""

import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

# cores
R, B   = "\033[0m", "\033[1m"
RED    = "\033[1;31m"
GRN    = "\033[1;32m"
YLW    = "\033[1;33m"
CYN    = "\033[1;36m"

DEVNULL = subprocess.DEVNULL


def info(msg): print(f"{GRN}[*]{R} {msg}")
def warn(msg): print(f"{YLW}[!]{R} {msg}")
def err(msg):  print(f"{RED}[x]{R} {msg}", file=sys.stderr)


def die(msg):
    err(msg)
    sys.exit(1)


# --------------------------------------------------------------- utilitarios
def out(cmd):
    """Roda e devolve stdout (str), engolindo erros."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=DEVNULL, text=True)
    except FileNotFoundError:
        return ""
    return p.stdout.strip()


def run(cmd, check=False, **kw):
    return subprocess.run(cmd, check=check, **kw)


def real_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"


def real_home():
    try:
        return Path(pwd.getpwnam(real_user()).pw_dir)
    except KeyError:
        return Path.home()


def http_up(url, timeout=2):
    """True se algo responde HTTP em `url` (qualquer status conta)."""
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_http(url, tries, delay):
    for _ in range(tries):
        if http_up(url):
            return True
        time.sleep(delay)
    return False


def open_browser(url):
    opener = shutil.which("xdg-open") or shutil.which("wslview")
    if not opener:
        return
    ru = os.environ.get("SUDO_USER")
    cmd = ["runuser", "-u", ru, "--", opener, url] if ru and os.geteuid() == 0 else [opener, url]
    try:
        subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
    except OSError:
        pass


# ------------------------------------------------------------- area de dados
BASE = SESS_DIR = ACTIVE_F = LOG_F = CONFIG_F = None


def init_paths():
    global BASE, SESS_DIR, ACTIVE_F, LOG_F, CONFIG_F
    BASE     = real_home() / ".bloodhound-sessions"
    SESS_DIR = BASE / "sessions"
    ACTIVE_F = BASE / "active"
    LOG_F    = BASE / "logs" / "bhapi.log"
    CONFIG_F = BASE / "config.json"
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_F.parent.mkdir(parents=True, exist_ok=True)
    # devolve a posse ao usuario real (se criamos como root)
    try:
        pw = pwd.getpwnam(real_user())
        for p in (BASE, SESS_DIR, LOG_F.parent):
            os.chown(p, pw.pw_uid, pw.pw_gid)
    except (KeyError, PermissionError):
        pass


def session_path(name):
    return SESS_DIR / name


def valid_name(name):
    return bool(name) and "/" not in name and not name.startswith(".")


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


def load_user_config():
    try:
        return json.loads(CONFIG_F.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_user_config(cfg):
    CONFIG_F.write_text(json.dumps(cfg, indent=2) + "\n")
    chown_real(CONFIG_F)


# =================================================================== backends
class Backend(ABC):
    """Primitivas que os comandos precisam. Neo4j precisa estar PARADO para
    snapshot/restore/clean; PostgreSQL precisa estar NO AR para dump/restore."""

    name = "?"
    needs_root = False
    url = "http://127.0.0.1:8080"

    @abstractmethod
    def check(self): ...

    @abstractmethod
    def ensure_postgres(self): ...

    @abstractmethod
    def start_app(self):
        """Sobe Neo4j + BloodHound (PostgreSQL ja deve estar no ar)."""

    @abstractmethod
    def stop_app(self):
        """Para BloodHound + Neo4j, deixando o PostgreSQL de pe."""

    @abstractmethod
    def stop_all(self): ...

    @abstractmethod
    def pg_recreate_empty(self): ...

    @abstractmethod
    def pg_dump_to(self, dest: Path): ...

    @abstractmethod
    def pg_restore_from(self, src: Path): ...

    @abstractmethod
    def neo4j_snapshot_to(self, dest: Path): ...

    @abstractmethod
    def neo4j_restore_from(self, src: Path): ...

    @abstractmethod
    def neo4j_clean_graph(self): ...

    @abstractmethod
    def status_lines(self):
        """Lista de (rotulo, valor) para o `status`."""

    def login_hint(self):
        return "Login padrao: admin / admin"

    stop_note = ""


# ---------------------------------------------------------------- local (Kali)
class LocalBackend(Backend):
    name = "local"
    needs_root = True
    stop_note = " (PostgreSQL continua de pe para outros usos)"

    PG_SUPER   = "postgres"                     # usuario de sistema do PostgreSQL
    NEO4J_CONF = Path("/etc/neo4j/neo4j.conf")
    NEO4J_HTTP = "http://localhost:7474/"
    BHAPI_BIN  = "./bhapi"
    BHAPI_USER = "_bloodhound"
    BHAPI_PROC = "bhapi"                        # nome do processo (comm) p/ pkill -x
    BHAPI_CONF = Path("/etc/bhapi/bhapi.json")
    BHAPI_DIRS = ("/usr/lib/bloodhound/bin", "/usr/share/bloodhound/bin")

    @classmethod
    def available(cls):
        return shutil.which("neo4j") is not None or any(
            Path(d, "bhapi").exists() for d in cls.BHAPI_DIRS)

    def __init__(self):
        # fallbacks; sobrescritos pelos arquivos do proprio BloodHound
        self.pg_db       = "bloodhound"
        self.pg_owner    = "_bloodhound"
        self.pg_password = "bloodhound"
        self.bhapi_dir   = self.BHAPI_DIRS[0]
        self.neo4j_data  = Path("/etc/neo4j/data")
        self._load_config()

    # ------------------------------------------------------------ config
    def _load_config(self):
        try:
            db = json.loads(self.BHAPI_CONF.read_text()).get("database", {})
            self.pg_db       = db.get("database", self.pg_db)
            self.pg_owner    = db.get("username", self.pg_owner)
            self.pg_password = db.get("secret", self.pg_password)
        except (json.JSONDecodeError, OSError):
            pass
        self.neo4j_data = self._detect_neo4j_data()
        for cand in self.BHAPI_DIRS:
            if Path(cand, "bhapi").exists():
                self.bhapi_dir = cand
                break

    def _detect_neo4j_data(self):
        """Descobre o diretorio de dados do Neo4j a partir do neo4j.conf."""
        data_val, home = None, Path("/usr/share/neo4j")
        try:
            lines = self.NEO4J_CONF.read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
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

    def check(self):
        missing = [b for b in ("neo4j", "pg_lsclusters", "runuser", "psql") if not shutil.which(b)]
        if missing:
            die(f"Faltam binarios: {', '.join(missing)}. O pacote `bloodhound` esta instalado?")
        if not Path(self.bhapi_dir, "bhapi").exists():
            die(f"bhapi nao encontrado em {self.bhapi_dir}. Instale: sudo apt install bloodhound")

    # -------------------------------------------------------- PostgreSQL
    def _as_pg(self, args, **kw):
        """Roda um comando do PostgreSQL como o usuario de sistema do banco."""
        return run(["runuser", "-u", self.PG_SUPER, "--", *args], **kw)

    def pg_cluster(self):
        """(versao, cluster, porta, status) do primeiro cluster PostgreSQL."""
        for line in out(["pg_lsclusters", "--no-header"]).splitlines():
            cols = line.split()
            if len(cols) >= 4:
                return cols[0], cols[1], cols[2], cols[3]
        return None

    def ensure_postgres(self):
        cl = self.pg_cluster()
        if not cl:
            die("Nenhum cluster PostgreSQL encontrado.")
        ver, cluster, port, status = cl
        if status != "online":
            info(f"Subindo cluster PostgreSQL {ver}/{cluster} (porta {port})")
            run(["pg_ctlcluster", ver, cluster, "start"])
        for _ in range(30):
            if run(["pg_isready", "-p", port], stdout=DEVNULL, stderr=DEVNULL).returncode == 0:
                return
            time.sleep(0.5)
        warn("PostgreSQL nao respondeu a pg_isready a tempo (seguindo assim mesmo).")

    def _pg_kill_connections(self):
        self._as_pg(["psql", "-d", "postgres", "-c",
                     f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     f"WHERE datname='{self.pg_db}' AND pid<>pg_backend_pid();"],
                    stdout=DEVNULL, stderr=DEVNULL)

    def _pg_set_password(self):
        self._as_pg(["psql", "-d", self.pg_db, "-c",
                     f"ALTER USER \"{self.pg_owner}\" WITH PASSWORD '{self.pg_password}';"],
                    stdout=DEVNULL, stderr=DEVNULL)

    def _pg_drop_create(self):
        self._pg_kill_connections()
        self._as_pg(["dropdb", "--if-exists", self.pg_db], stdout=DEVNULL, stderr=DEVNULL)
        self._as_pg(["createdb", "-O", self.pg_owner, self.pg_db], check=True)

    def pg_recreate_empty(self):
        """Dropa e recria o banco vazio; o bhapi migra o schema no proximo boot."""
        self._pg_drop_create()
        self._pg_set_password()

    def pg_dump_to(self, dest: Path):
        with open(dest, "wb") as f:
            p = self._as_pg(["pg_dump", "-Fc", self.pg_db], stdout=f)
        if p.returncode != 0:
            raise RuntimeError("pg_dump falhou")

    def pg_restore_from(self, src: Path):
        self._pg_drop_create()
        with open(src, "rb") as f:
            self._as_pg(["pg_restore", "--no-owner", f"--role={self.pg_owner}", "-d", self.pg_db],
                        stdin=f, stdout=DEVNULL, stderr=DEVNULL)
        self._pg_set_password()

    # ------------------------------------------------------------- Neo4j
    def neo4j_running(self):
        p = run(["neo4j", "status"], stdout=subprocess.PIPE, stderr=DEVNULL, text=True)
        return p.returncode == 0 and "is running" in (p.stdout or "")

    def _neo4j_start(self):
        if not self.neo4j_running():
            info("Subindo Neo4j")
            run(["neo4j", "start"])
        if not wait_http(self.NEO4J_HTTP, 120, 0.5):
            warn("Neo4j HTTP nao respondeu a tempo (seguindo assim mesmo).")

    def _neo4j_stop(self):
        if self.neo4j_running():
            info("Parando Neo4j")
            run(["neo4j", "stop"])
            for _ in range(60):
                if not self.neo4j_running():
                    break
                time.sleep(0.5)

    def neo4j_snapshot_to(self, dest: Path):
        """tar.gz de TODO o diretorio de dados (Neo4j precisa estar parado)."""
        run(["tar", "czf", str(dest), "-C", str(self.neo4j_data), "."], check=True)

    def neo4j_restore_from(self, src: Path):
        st = self.neo4j_data.stat()
        for child in self.neo4j_data.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        run(["tar", "xzf", str(src), "-C", str(self.neo4j_data)], check=True)
        run(["chown", "-R", f"{st.st_uid}:{st.st_gid}", str(self.neo4j_data)])

    def neo4j_clean_graph(self):
        """Apaga so o grafo `neo4j`, preservando o db `system` (autenticacao)."""
        for sub in ("databases/neo4j", "transactions/neo4j"):
            p = self.neo4j_data / sub
            if p.exists():
                shutil.rmtree(p)

    # ------------------------------------------------------------- bhapi
    def bhapi_running(self):
        return run(["pgrep", "-x", self.BHAPI_PROC], stdout=DEVNULL).returncode == 0

    def _bhapi_start(self):
        if self.bhapi_running():
            info("bhapi ja esta rodando")
            return
        info("Subindo bhapi (pode demorar um pouco...)")
        logf = open(LOG_F, "ab")
        subprocess.Popen(["runuser", "-u", self.BHAPI_USER, "--", self.BHAPI_BIN],
                         cwd=self.bhapi_dir, stdout=logf, stderr=logf, start_new_session=True)
        if not wait_http(self.url, 180, 1):
            warn(f"bhapi nao respondeu em {self.url} a tempo. Veja o log: {LOG_F}")

    def _bhapi_stop(self):
        if self.bhapi_running():
            info("Parando bhapi")
            run(["pkill", "-x", self.BHAPI_PROC])
            for _ in range(30):
                if not self.bhapi_running():
                    break
                time.sleep(0.5)

    # ----------------------------------------------------------- servicos
    def start_app(self):
        self._neo4j_start()
        self._bhapi_start()

    def stop_app(self):
        self._bhapi_stop()
        self._neo4j_stop()

    def stop_all(self):
        self.stop_app()

    def status_lines(self):
        cl = self.pg_cluster()
        pg = f"{cl[0]}/{cl[1]} porta {cl[2]} [{cl[3]}]" if cl else "nao encontrado"
        return [
            ("PostgreSQL", f"{pg}  (db={self.pg_db})"),
            ("Neo4j", f"{'rodando' if self.neo4j_running() else 'parado'}  (data={self.neo4j_data})"),
            ("bhapi", "rodando" if self.bhapi_running() else "parado"),
        ]


# ------------------------------------------------------------- Docker Compose
class DockerBackend(Backend):
    name = "docker"
    needs_root = False
    stop_note = " (containers parados; volumes preservados)"

    SVC_PG    = "app-db"
    SVC_NEO4J = "graph-db"
    SVC_BH    = "bloodhound"

    def __init__(self, compose_file: Path):
        self.compose_file = compose_file
        self.dc = ["docker", "compose", "-f", str(compose_file)]
        self.project = compose_file.parent.name
        self.pg_user = self.pg_db = "bloodhound"
        self.neo4j_volume = f"{self.project}_neo4j-data"

    def _dc(self, *args, **kw):
        return run(self.dc + list(args), **kw)

    def check(self):
        if not shutil.which("docker"):
            die("`docker` nao encontrado no PATH.")
        if run(["docker", "info"], stdout=DEVNULL, stderr=DEVNULL).returncode != 0:
            die("Sem acesso ao daemon Docker (daemon parado? usuario fora do grupo `docker`?).")
        p = run(self.dc + ["config", "--format", "json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            die(f"`docker compose config` falhou para {self.compose_file}:\n{p.stderr.strip()}")
        cfg = json.loads(p.stdout)
        self.project = cfg.get("name", self.project)
        svcs = cfg.get("services", {})
        missing = {self.SVC_PG, self.SVC_NEO4J, self.SVC_BH} - set(svcs)
        if missing:
            die(f"Compose sem os servicos esperados: {', '.join(sorted(missing))} "
                f"(use o docker-compose oficial da SpecterOps).")

        env = svcs[self.SVC_PG].get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) for e in env if "=" in e)
        self.pg_user = env.get("POSTGRES_USER") or "postgres"
        self.pg_db   = env.get("POSTGRES_DB") or self.pg_user

        for port in svcs[self.SVC_BH].get("ports") or []:
            if isinstance(port, dict) and int(port.get("target", 0)) == 8080 and port.get("published"):
                host = port.get("host_ip") or "127.0.0.1"
                if host in ("0.0.0.0", "::"):
                    host = "127.0.0.1"
                self.url = f"http://{host}:{port['published']}"

        for v in svcs[self.SVC_NEO4J].get("volumes") or []:
            if isinstance(v, dict) and v.get("target") == "/data":
                src = v.get("source", "")
                if v.get("type") == "volume":
                    self.neo4j_volume = (cfg.get("volumes", {}).get(src) or {}).get(
                        "name", f"{self.project}_{src}")
                else:
                    self.neo4j_volume = src

    # -------------------------------------------------------- PostgreSQL
    def _pg_exec(self, args, **kw):
        return self._dc("exec", "-T", self.SVC_PG, *args, **kw)

    def ensure_postgres(self):
        if self._dc("up", "-d", "--wait", self.SVC_PG).returncode != 0:
            die(f"Nao consegui subir o servico {self.SVC_PG}.")

    def pg_recreate_empty(self):
        """Dropa e recria o banco vazio; o BloodHound migra o schema no proximo boot."""
        self._pg_exec(["dropdb", "-U", self.pg_user, "--if-exists", "--force", self.pg_db],
                      stdout=DEVNULL, stderr=DEVNULL)
        if self._pg_exec(["createdb", "-U", self.pg_user, "-O", self.pg_user, self.pg_db]).returncode != 0:
            raise RuntimeError("createdb falhou")

    def pg_dump_to(self, dest: Path):
        with open(dest, "wb") as f:
            p = self._pg_exec(["pg_dump", "-U", self.pg_user, "-Fc", self.pg_db], stdout=f)
        if p.returncode != 0:
            raise RuntimeError("pg_dump falhou")

    def pg_restore_from(self, src: Path):
        self.pg_recreate_empty()
        with open(src, "rb") as f:
            p = self._pg_exec(["pg_restore", "-U", self.pg_user, "--no-owner", "--no-acl",
                               f"--role={self.pg_user}", "-d", self.pg_db],
                              stdin=f, stdout=DEVNULL, stderr=DEVNULL)
        if p.returncode != 0:
            warn(f"pg_restore terminou com avisos (rc={p.returncode}); normal entre backends diferentes.")

    # ------------------------------------------------------------- Neo4j
    def _neo4j_volume_sh(self, script, backup_dir=None, readonly=False):
        """Roda `sh -c script` num container descartavel do graph-db, com o
        mesmo volume /data montado (o graph-db precisa estar parado)."""
        args = ["run", "--rm", "--no-deps", "-T", "--entrypoint", "sh"]
        if backup_dir:
            args += ["-v", f"{backup_dir}:/backup" + (":ro" if readonly else "")]
        args += [self.SVC_NEO4J, "-c", script]
        if self._dc(*args, stdout=DEVNULL).returncode != 0:
            raise RuntimeError(f"operacao no volume do Neo4j falhou: {script}")

    def neo4j_snapshot_to(self, dest: Path):
        f = shlex.quote(f"/backup/{dest.name}")
        self._neo4j_volume_sh(
            f"tar czf {f} -C /data . && chown {os.getuid()}:{os.getgid()} {f}",
            backup_dir=dest.parent.resolve())

    def neo4j_restore_from(self, src: Path):
        f = shlex.quote(f"/backup/{src.name}")
        self._neo4j_volume_sh(
            f"find /data -mindepth 1 -delete && tar xzf {f} -C /data "
            f"&& chown -R \"$(stat -c %u:%g /data)\" /data",
            backup_dir=src.parent.resolve(), readonly=True)

    def neo4j_clean_graph(self):
        self._neo4j_volume_sh("rm -rf /data/databases/neo4j /data/transactions/neo4j")

    # ----------------------------------------------------------- servicos
    def start_app(self):
        info("Subindo containers (pode demorar um pouco...)")
        if self._dc("up", "-d", "--wait").returncode != 0:
            warn("`docker compose up --wait` reportou falha; veja `docker compose logs`.")
        if not wait_http(self.url, 180, 1):
            warn(f"BloodHound nao respondeu em {self.url} a tempo. "
                 f"Veja: docker compose -f {self.compose_file} logs {self.SVC_BH}")

    def stop_app(self):
        info("Parando bloodhound + graph-db")
        self._dc("stop", self.SVC_BH, self.SVC_NEO4J, stderr=DEVNULL)

    def stop_all(self):
        info("Parando todos os containers")
        self._dc("stop", stderr=DEVNULL)

    def login_hint(self):
        logs = out(self.dc + ["logs", "--no-log-prefix", "--tail", "500", self.SVC_BH])
        pw = [l.split(":", 1)[1].strip(" #") for l in logs.splitlines()
              if "Initial Password Set To:" in l]
        if pw:
            return f"Login: admin / {pw[-1]}"
        return f"Login: admin (senha inicial: docker compose -f {self.compose_file} logs {self.SVC_BH})"

    def _ps(self):
        raw = out(self.dc + ["ps", "-a", "--format", "json"])
        try:
            rows = json.loads(raw) if raw.startswith("[") else [
                json.loads(l) for l in raw.splitlines() if l.strip()]
        except json.JSONDecodeError:
            rows = []
        return {r.get("Service"): r for r in rows}

    def status_lines(self):
        ps = self._ps()
        lines = [("Compose", str(self.compose_file)), ("Projeto", self.project)]
        for svc in (self.SVC_PG, self.SVC_NEO4J, self.SVC_BH):
            r = ps.get(svc)
            if not r:
                state = "sem container"
            else:
                state = r.get("State", "?")
                if r.get("Health"):
                    state += f" ({r['Health']})"
            lines.append((svc, state))
        lines.append(("Volume Neo4j", self.neo4j_volume))
        lines.append(("PG db/user", f"{self.pg_db} / {self.pg_user}"))
        return lines


# ------------------------------------------------------- selecao de backend
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def is_bh_compose(p: Path):
    try:
        return p.is_file() and "specterops/bloodhound" in p.read_text(errors="ignore")
    except OSError:
        return False


def find_compose_file():
    """Procura um compose do BloodHound subindo a partir do CWD (e em ./bloodhound),
    depois em ~/bloodhound e ~/.config/bloodhound."""
    cwd = Path.cwd()
    dirs = [x for d in (cwd, *cwd.parents) for x in (d, d / "bloodhound")]
    home = real_home()
    dirs += [home / "bloodhound", home / ".config" / "bloodhound"]
    for d in dirs:
        for n in COMPOSE_NAMES:
            if is_bh_compose(d / n):
                return d / n
    return None


def parse_global_flags(argv):
    backend, compose, rest = None, None, []
    it = iter(argv)
    for a in it:
        if a in ("--docker", "--local"):
            backend = a[2:]
        elif a == "--compose-file":
            compose = next(it, None)
            if not compose:
                die("--compose-file precisa de um caminho")
        elif a.startswith("--compose-file="):
            compose = a.split("=", 1)[1]
        else:
            rest.append(a)
    if compose and backend is None:
        backend = "docker"
    if compose and backend == "local":
        die("--compose-file so faz sentido com --docker")
    return backend, compose, rest


def resolve_backend(flag, compose_flag):
    cfg = load_user_config()
    choice = flag or os.environ.get("BH_BACKEND") or cfg.get("backend")
    compose = compose_flag or os.environ.get("BH_COMPOSE_FILE") or cfg.get("compose_file")

    if choice not in (None, "docker", "local"):
        die(f"backend invalido: {choice!r} (use docker ou local)")

    if choice is None:
        compose = compose or find_compose_file()
        has_docker = shutil.which("docker") is not None
        if compose and has_docker:
            choice = "docker"
        elif LocalBackend.available():
            if compose:
                warn(f"Compose do BloodHound encontrado ({compose}), mas `docker` nao esta "
                     f"instalado; usando o pacote local.")
            choice = "local"
        elif compose:
            die(f"Compose do BloodHound encontrado ({compose}), mas `docker` nao esta instalado. "
                f"Instale o Docker (com o plugin `docker compose`) ou o pacote "
                f"(`sudo apt install bloodhound`).")
        else:
            die("Nenhum BloodHound encontrado: nem `docker` + compose do BloodHound, nem o "
                "pacote Kali. Instale o pacote (`sudo apt install bloodhound`) ou aponte o "
                "compose: --compose-file PATH")

    if choice == "docker":
        compose = compose or find_compose_file()
        if not compose:
            die("docker-compose do BloodHound nao encontrado. Use --compose-file PATH "
                "ou BH_COMPOSE_FILE=PATH")
        compose = Path(compose).expanduser().resolve()
        if not compose.is_file():
            die(f"compose nao existe: {compose}")
        backend = DockerBackend(compose)
    else:
        backend = LocalBackend()

    if flag:
        cfg["backend"] = choice
        if choice == "docker":
            cfg["compose_file"] = str(compose)
        save_user_config(cfg)
    return backend


# -------------------------------------------------------------------- helpers
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
    return {"name": name, "description": "", "created": None, "updated": None}


def snapshot_to(be, name, reason=""):
    """Salva o estado VIVO atual na sessao `name` (Neo4j parado p/ consistencia)."""
    sp = session_path(name)
    sp.mkdir(parents=True, exist_ok=True)
    chown_real(sp)
    info(f"Salvando estado em '{name}' {reason}".rstrip())
    be.stop_app()
    be.ensure_postgres()
    be.pg_dump_to(sp / "pg.dump")
    be.neo4j_snapshot_to(sp / "neo4j_data.tar.gz")
    meta = read_meta(name)
    write_meta(name, meta.get("description", ""), meta.get("created"))
    chown_real(sp)


def snapshot_active(be, reason=""):
    """Persiste a sessao ativa (se houver uma nomeada) antes de algo destrutivo."""
    name = active_session()
    if name:
        snapshot_to(be, name, reason)
    else:
        warn("Sessao atual sem nome (scratch): dados nao salvos serao descartados.")


def require_existing(name, usage):
    if not name or not valid_name(name):
        die(usage if not name else f"nome invalido: {name}")
    if not session_path(name).exists():
        die(f"Sessao '{name}' nao existe. Veja 'list'.")


# -------------------------------------------------------------------- comandos
def cmd_up(args, be):
    name = active_session()
    be.ensure_postgres()
    be.start_app()
    print()
    label = f"{CYN}{name}{R}" if name else f"{YLW}(scratch, sem nome){R}"
    info(f"BloodHound no ar  |  sessao: {label}  |  {be.url}")
    open_browser(be.url)


def cmd_new(args, be):
    if not args or not valid_name(args[0]):
        die("uso: BH_Handler.py new <nome> [descricao]")
    name, desc = args[0], " ".join(args[1:])
    if session_path(name).exists():
        die(f"Sessao '{name}' ja existe. Use 'use {name}' ou outro nome.")
    snapshot_active(be, reason="(antes de criar a nova)")
    info(f"Criando sessao LIMPA '{name}'")
    be.stop_app()
    be.ensure_postgres()
    be.pg_recreate_empty()
    be.neo4j_clean_graph()
    session_path(name).mkdir(parents=True, exist_ok=True)
    write_meta(name, desc)
    set_active(name)
    chown_real(session_path(name))
    be.start_app()
    print()
    info(f"Sessao '{name}' criada, vazia e ativa. {be.url}")
    info(be.login_hint())
    open_browser(be.url)


def cmd_clean(args, be):
    snapshot_active(be, reason="(antes de limpar)")
    info("Zerando os bancos (scratch limpo)")
    be.stop_app()
    be.ensure_postgres()
    be.pg_recreate_empty()
    be.neo4j_clean_graph()
    set_active(None)
    be.start_app()
    print()
    info(f"BloodHound limpo e no ar (scratch, sem nome). {be.url}")
    info(be.login_hint())
    open_browser(be.url)


def cmd_use(args, be):
    name = args[0] if args else None
    require_existing(name, "uso: BH_Handler.py use <nome>")
    if active_session() == name:
        info(f"'{name}' ja e a ativa. Subindo...")
        cmd_up([], be)
        return
    snapshot_active(be, reason="(antes de trocar)")
    info(f"Carregando sessao '{name}'")
    be.stop_app()
    be.ensure_postgres()
    sp = session_path(name)
    if (sp / "pg.dump").exists():
        be.pg_restore_from(sp / "pg.dump")
    else:
        warn("sessao sem dump PostgreSQL; criando banco vazio")
        be.pg_recreate_empty()
    if (sp / "neo4j_data.tar.gz").exists():
        be.neo4j_restore_from(sp / "neo4j_data.tar.gz")
    else:
        warn("sessao sem snapshot Neo4j; limpando grafo")
        be.neo4j_clean_graph()
    set_active(name)
    be.start_app()
    print()
    info(f"Sessao '{name}' ativa. {be.url}")
    open_browser(be.url)


def cmd_save(args, be):
    name = args[0] if args else active_session()
    if not name:
        die("Sem sessao ativa. Use: BH_Handler.py save <nome>")
    if not valid_name(name):
        die(f"nome invalido: {name}")
    is_new = not session_path(name).exists()
    snapshot_to(be, name, reason="(checkpoint manual)")
    if args:
        set_active(name)
    info(f"{'Criada e salva' if is_new else 'Salva'} a sessao '{name}'. Reabrindo...")
    be.start_app()


def cmd_list(args, be=None):
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


def cmd_del(args, be=None):
    name = args[0] if args else None
    require_existing(name, "uso: BH_Handler.py del <nome>")
    if active_session() == name:
        die(f"'{name}' e a sessao ATIVA. Troque com 'use <outra>' ou 'clean' antes de apagar.")
    shutil.rmtree(session_path(name))
    info(f"Sessao '{name}' removida.")


def cmd_rename(args, be=None):
    if len(args) < 2 or not valid_name(args[1]):
        die("uso: BH_Handler.py rename <antigo> <novo>")
    old, new = args[0], args[1]
    require_existing(old, "uso: BH_Handler.py rename <antigo> <novo>")
    if session_path(new).exists():
        die(f"Sessao '{new}' ja existe.")
    session_path(old).rename(session_path(new))
    meta = read_meta(new)
    write_meta(new, meta.get("description", ""), meta.get("created"))
    if active_session() == old:
        set_active(new)
    info(f"'{old}' -> '{new}'")


def cmd_stop(args, be):
    be.stop_all()
    info(f"Servicos parados{be.stop_note}.")


def cmd_status(args, be):
    act = active_session()
    print(f"\n{B}Status BloodHound{R}")
    print(f"  {'Backend':<13}: {CYN}{be.name}{R}")
    print(f"  {'Sessao ativa':<13}: " + (f"{CYN}{act}{R}" if act else f"{YLW}(scratch, sem nome){R}"))
    for label, value in be.status_lines():
        print(f"  {label:<13}: {value}")
    print(f"  {'URL':<13}: {be.url}  [{'no ar' if http_up(be.url) else 'fora do ar'}]")
    print(f"  {'Sessoes em':<13}: {SESS_DIR}\n")


def cmd_help(args=None, be=None):
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

# so mexem nos arquivos de sessao: nao precisam de backend nem de root
NO_BACKEND = {cmd_list, cmd_del, cmd_rename}


def main():
    flag, compose_flag, argv = parse_global_flags(sys.argv[1:])

    if not argv or argv[0] in ("help", "-h", "--help"):
        cmd_help()
        return

    handler = COMMANDS.get(argv[0])
    if not handler:
        err(f"comando desconhecido: {argv[0]}\n")
        cmd_help()
        sys.exit(1)

    init_paths()
    if handler in NO_BACKEND:
        handler(argv[1:])
        return

    be = resolve_backend(flag, compose_flag)
    # valida antes do sudo: falta de pacote/docker nao deve pedir senha
    be.check()
    if be.needs_root and os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-E", sys.executable, os.path.abspath(__file__), *sys.argv[1:]])

    try:
        handler(argv[1:], be)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        die(str(e))


if __name__ == "__main__":
    main()
