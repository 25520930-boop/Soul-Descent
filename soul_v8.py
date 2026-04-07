import pygame, random, math, sys, json, os, threading, socket, struct, time, pickle
from pygame.math import Vector2

W, H = 1024, 768
FPS  = 60
TILE = 40
pygame.init()
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
screen = pygame.display.set_mode((W, H))
pygame.display.set_caption("Soul Descent")
clock = pygame.time.Clock()

# ─── LAN MULTIPLAYER ENGINE ───────────────────────────────────────────────────
NET_PORT   = 7788
NET_TICK   = 1/30   # 30 updates/sec
MAX_PEERS  = 4
COLORS_MP  = [(60,140,220),(220,80,60),(60,200,80),(220,180,40)]  # Blue Red Green Yellow

class NetMsg:
    # Message types
    HELLO       = 0   # client → host: join request {name, class}
    WELCOME     = 1   # host → client: your pid + seed + all peers
    PEER_JOIN   = 2   # host → all: new peer joined
    PEER_LEAVE  = 3   # host → all: peer disconnected
    INPUT       = 4   # client → host: player input state
    STATE       = 5   # host → all: full game state snapshot
    CHAT        = 6   # any → all: chat message
    REVIVE_REQ  = 7   # client → host: request revive target
    PING        = 8   # bidirectional keepalive
    START       = 9   # host → all: game starting

    @staticmethod
    def pack(msg_type, data):
        raw = pickle.dumps({"t": msg_type, "d": data})
        return struct.pack("!I", len(raw)) + raw

    @staticmethod
    def unpack(raw):
        try:
            return pickle.loads(raw[4:])
        except: return None

class NetHost:
    """Runs on host machine. Manages all clients."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", NET_PORT))
        self.sock.setblocking(False)
        self.peers   = {}   # addr → {pid, name, class, last_seen, input}
        self.pid_ctr = 0    # 0 = host
        self.running = True
        self.seed    = random.randint(0, 999999)
        self.inputs  = {}   # pid → input dict
        self.chat    = []   # [(pid, msg), ...]
        self._lock   = threading.Lock()
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        while self.running:
            try:
                raw, addr = self.sock.recvfrom(65535)
                msg = pickle.loads(raw)
                t = msg["t"]; d = msg["d"]
                with self._lock:
                    if t == NetMsg.HELLO:
                        if addr not in self.peers and len(self.peers) < MAX_PEERS-1:
                            self.pid_ctr += 1
                            pid = self.pid_ctr
                            self.peers[addr] = {"pid":pid,"name":d["name"],"class":d["cls"],
                                                "last_seen":time.time(),"input":{}}
                            # Send WELCOME to new peer
                            welcome = {"pid":pid,"seed":self.seed,
                                       "peers":[{"pid":p["pid"],"name":p["name"],"class":p["class"]}
                                                for p in self.peers.values()]}
                            self._send(addr, NetMsg.WELCOME, welcome)
                            # Notify others
                            for a2 in self.peers:
                                if a2 != addr:
                                    self._send(a2, NetMsg.PEER_JOIN,
                                               {"pid":pid,"name":d["name"],"class":d["cls"]})
                    elif t == NetMsg.INPUT:
                        if addr in self.peers:
                            self.peers[addr]["input"] = d
                            self.peers[addr]["last_seen"] = time.time()
                            pid = self.peers[addr]["pid"]
                            self.inputs[pid] = d
                    elif t == NetMsg.CHAT:
                        if addr in self.peers:
                            pid = self.peers[addr]["pid"]
                            self.chat.append((pid, d["msg"]))
                            for a2 in self.peers:
                                self._send(a2, NetMsg.CHAT, {"pid":pid,"msg":d["msg"]})
                    elif t == NetMsg.PING:
                        if addr in self.peers:
                            self.peers[addr]["last_seen"] = time.time()
                            self._send(addr, NetMsg.PING, {})
            except (BlockingIOError, OSError): pass
            except Exception: pass
            # Timeout check
            now = time.time()
            with self._lock:
                dead = [a for a,p in self.peers.items() if now-p["last_seen"]>5]
                for a in dead:
                    pid = self.peers[a]["pid"]
                    del self.peers[a]
                    for a2 in self.peers:
                        self._send(a2, NetMsg.PEER_LEAVE, {"pid":pid})

    def _send(self, addr, t, d):
        try:
            raw = pickle.dumps({"t":t,"d":d})
            self.sock.sendto(raw, addr)
        except: pass

    def broadcast_state(self, state):
        with self._lock:
            for addr in self.peers:
                self._send(addr, NetMsg.STATE, state)

    def broadcast_start(self):
        with self._lock:
            for addr in self.peers:
                self._send(addr, NetMsg.START, {"seed":self.seed})

    def get_peer_list(self):
        with self._lock:
            return [{"pid":p["pid"],"name":p["name"],"class":p["class"]}
                    for p in self.peers.values()]

    def get_inputs(self):
        with self._lock:
            return dict(self.inputs)

    def stop(self):
        self.running=False
        try: self.sock.close()
        except: pass

class NetClient:
    """Runs on client machine."""
    def __init__(self, host_ip, name, cls):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.host  = (host_ip, NET_PORT)
        self.pid   = None
        self.seed  = None
        self.peers = []
        self.state = None
        self.chat  = []
        self.connected = False
        self.running   = True
        self._lock = threading.Lock()
        self._last_ping = time.time()
        # Send HELLO
        raw = pickle.dumps({"t":NetMsg.HELLO,"d":{"name":name,"cls":cls}})
        self.sock.sendto(raw, self.host)
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        while self.running:
            try:
                raw, addr = self.sock.recvfrom(65535)
                msg = pickle.loads(raw)
                t = msg["t"]; d = msg["d"]
                with self._lock:
                    if t == NetMsg.WELCOME:
                        self.pid = d["pid"]; self.seed = d["seed"]
                        self.peers = d["peers"]; self.connected = True
                    elif t == NetMsg.PEER_JOIN:
                        self.peers.append(d)
                    elif t == NetMsg.PEER_LEAVE:
                        self.peers = [p for p in self.peers if p["pid"]!=d["pid"]]
                    elif t == NetMsg.STATE:
                        self.state = d
                    elif t == NetMsg.CHAT:
                        self.chat.append((d["pid"], d["msg"]))
                    elif t == NetMsg.START:
                        self.seed = d["seed"]; self.connected = True
            except (BlockingIOError, OSError): pass
            except Exception: pass
            # Keepalive ping
            if time.time()-self._last_ping > 1.5:
                self._last_ping = time.time()
                self.send(NetMsg.PING, {})

    def send(self, t, d):
        try:
            raw = pickle.dumps({"t":t,"d":d})
            self.sock.sendto(raw, self.host)
        except: pass

    def send_input(self, inp):
        self.send(NetMsg.INPUT, inp)

    def get_state(self):
        with self._lock:
            return self.state

    def stop(self):
        self.running=False
        try: self.sock.close()
        except: pass

def get_local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80)); ip=s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

# ─── LOBBY SCREEN ─────────────────────────────────────────────────────────────
def lobby_screen(surf):
    """Host/Join selection. Returns (mode, host_ip, player_name)."""
    mode     = None   # "host" or "join" or "solo"
    host_ip  = ""
    name_inp = ""
    active_field = "name"
    error    = ""
    t_anim   = 0.0
    clk2     = pygame.time.Clock()
    local_ip = get_local_ip()

    while mode is None:
        dt2 = clk2.tick(60)/1000.0; t_anim+=dt2
        surf.fill((6,4,10))

        # Animated title
        tc2=(int(180+math.sin(t_anim*2)*60),int(100+math.sin(t_anim*1.5)*50),220)
        t1=F_HUGE.render("SOUL DESCENT",True,tc2)
        surf.blit(t1,(W//2-t1.get_width()//2,40))
        sub=F_MED.render("Multiplayer Dungeon",True,C["gray"])
        surf.blit(sub,(W//2-sub.get_width()//2,105))

        mx,my=pygame.mouse.get_pos()

        # Name field
        surf.blit(F_MED.render("Your Name:",True,C["white"]),(W//2-220,180))
        name_rect=pygame.Rect(W//2-220,210,440,44)
        pygame.draw.rect(surf,(25,20,38),name_rect,border_radius=8)
        pygame.draw.rect(surf,C["cyan"] if active_field=="name" else C["gray"],name_rect,2,border_radius=8)
        display_name=name_inp if name_inp else "Enter name..."
        nc=C["white"] if name_inp else C["gray"]
        surf.blit(F_MED.render(display_name,True,nc),(W//2-210,220))
        # Cursor blink
        if active_field=="name" and int(t_anim*2)%2==0:
            cx2=W//2-210+F_MED.size(name_inp)[0]+2
            pygame.draw.line(surf,C["cyan"],(cx2,215),(cx2,245),2)

        # IP field
        surf.blit(F_MED.render("Host IP (for Join):",True,C["white"]),(W//2-220,275))
        ip_rect=pygame.Rect(W//2-220,305,440,44)
        pygame.draw.rect(surf,(25,20,38),ip_rect,border_radius=8)
        pygame.draw.rect(surf,C["orange"] if active_field=="ip" else C["gray"],ip_rect,2,border_radius=8)
        display_ip=host_ip if host_ip else "192.168.x.x"
        ic2=C["white"] if host_ip else C["gray"]
        surf.blit(F_MED.render(display_ip,True,ic2),(W//2-210,315))
        if active_field=="ip" and int(t_anim*2)%2==0:
            cx3=W//2-210+F_MED.size(host_ip)[0]+2
            pygame.draw.line(surf,C["orange"],(cx3,310),(cx3,340),2)

        # Your IP hint
        surf.blit(F_SM.render(f"Your IP: {local_ip}  (share this with friends to join)",True,(100,100,120)),(W//2-220,358))

        # Buttons
        buttons=[
            (W//2-330,400,200,55,"Host Game",C["cyan"],"host"),
            (W//2-90, 400,180,55,"Join Game",C["orange"],"join"),
            (W//2+110,400,160,55,"Solo Play",C["gray"],"solo"),
        ]
        for bx2,by2,bw2,bh2,label,col2,act in buttons:
            r2=pygame.Rect(bx2,by2,bw2,bh2); hov=r2.collidepoint(mx,my)
            pygame.draw.rect(surf,(30,24,44) if hov else (18,12,28),r2,border_radius=10)
            pygame.draw.rect(surf,col2,r2,2,border_radius=10)
            lt2=F_BIG.render(label,True,C["white"])
            surf.blit(lt2,(bx2+bw2//2-lt2.get_width()//2,by2+12))

        if error:
            surf.blit(F_MED.render(error,True,C["red"]),(W//2-220,470))

        # Controls hint
        surf.blit(F_SM.render("Tab = switch field   Enter = confirm   ESC = quit",True,(80,80,100)),(W//2-220,510))

        pygame.display.flip()

        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN:
                if ev.key==pygame.K_ESCAPE: sys.exit()
                elif ev.key==pygame.K_TAB:
                    active_field="ip" if active_field=="name" else "name"
                elif ev.key==pygame.K_RETURN:
                    if not name_inp.strip(): error="Please enter a name!"; continue
                    if active_field=="ip" or host_ip:
                        if not host_ip.strip(): error="Please enter host IP!"; continue
                        return "join", host_ip.strip(), name_inp.strip()
                    else:
                        return "host", local_ip, name_inp.strip()
                elif ev.key==pygame.K_BACKSPACE:
                    if active_field=="name": name_inp=name_inp[:-1]
                    else: host_ip=host_ip[:-1]
                else:
                    ch=ev.unicode
                    if active_field=="name" and len(name_inp)<16 and ch.isprintable():
                        name_inp+=ch
                    elif active_field=="ip" and len(host_ip)<20 and (ch.isdigit() or ch=="."):
                        host_ip+=ch
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                for bx2,by2,bw2,bh2,label,col2,act in buttons:
                    if pygame.Rect(bx2,by2,bw2,bh2).collidepoint(mx,my):
                        if not name_inp.strip(): error="Please enter a name!"; break
                        if act=="join" and not host_ip.strip(): error="Enter host IP first!"; break
                        if act=="host": return "host",local_ip,name_inp.strip()
                        if act=="join": return "join",host_ip.strip(),name_inp.strip()
                        if act=="solo": return "solo","",name_inp.strip()
                if name_rect.collidepoint(mx,my): active_field="name"
                if ip_rect.collidepoint(mx,my): active_field="ip"

def waiting_room(surf, net_host, player_name, player_class):
    """Host waits for others to join, can start when ready."""
    clk3=pygame.time.Clock(); t_anim=0.0
    while True:
        dt3=clk3.tick(60)/1000.0; t_anim+=dt3
        surf.fill((6,4,10))
        tc3=(int(180+math.sin(t_anim*2)*60),int(100+math.sin(t_anim*1.5)*50),220)
        t1=F_HUGE.render("WAITING ROOM",True,tc3)
        surf.blit(t1,(W//2-t1.get_width()//2,40))
        local_ip=get_local_ip()
        surf.blit(F_MED.render(f"Your IP: {local_ip}   Port: {NET_PORT}",True,C["cyan"]),(W//2-200,100))
        surf.blit(F_SM.render("Share your IP with friends so they can join",True,C["gray"]),(W//2-200,130))

        # Player list
        peers=net_host.get_peer_list()
        all_players=[{"pid":0,"name":player_name,"class":player_class}]+peers
        surf.blit(F_MED.render(f"Players ({len(all_players)}/{MAX_PEERS}):",True,C["white"]),(W//2-200,170))
        for i,p in enumerate(all_players):
            col_p=COLORS_MP[p["pid"]%len(COLORS_MP)]
            cd_p=CLASSES.get(p["class"],{}); cls_col=cd_p.get("col",C["white"])
            y2=210+i*52
            pygame.draw.rect(surf,(20,16,30),(W//2-200,y2,400,44),border_radius=8)
            pygame.draw.rect(surf,col_p,(W//2-200,y2,400,44),2,border_radius=8)
            tag="[HOST] " if p["pid"]==0 else ""
            surf.blit(F_MED.render(f"{tag}{p['name']}",True,col_p),(W//2-188,y2+4))
            surf.blit(F_SM.render(p["class"],True,cls_col),(W//2-188,y2+26))
            # Ready dot
            pygame.draw.circle(surf,C["green"],(W//2+180,y2+22),7)

        mx,my=pygame.mouse.get_pos()
        # Start button
        can_start=len(all_players)>=1
        start_r=pygame.Rect(W//2-100,H-120,200,55)
        hov_s=start_r.collidepoint(mx,my)
        pygame.draw.rect(surf,C["green"] if (hov_s and can_start) else (30,24,44),start_r,border_radius=10)
        pygame.draw.rect(surf,C["green"] if can_start else C["gray"],start_r,2,border_radius=10)
        st=F_BIG.render("START GAME",True,C["white"])
        surf.blit(st,(W//2-st.get_width()//2,H-108))
        if len(all_players)==1:
            surf.blit(F_SM.render("(can start solo or wait for friends)",True,C["gray"]),(W//2-150,H-56))

        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN and ev.key==pygame.K_ESCAPE: sys.exit()
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                if start_r.collidepoint(mx,my) and can_start:
                    net_host.broadcast_start()
                    return all_players
        clk3.tick(60)

def joining_screen(surf, net_client):
    """Client waits to connect."""
    clk4=pygame.time.Clock(); t_anim=0.0; start_t=time.time()
    while not net_client.connected:
        dt4=clk4.tick(60)/1000.0; t_anim+=dt4
        surf.fill((6,4,10))
        dots="."*int(t_anim*2%4)
        surf.blit(F_HUGE.render(f"Connecting{dots}",True,C["cyan"]),(W//2-200,H//2-40))
        surf.blit(F_MED.render(f"Waiting for host response...",True,C["gray"]),(W//2-160,H//2+30))
        if time.time()-start_t>8:
            surf.blit(F_MED.render("Timeout — check IP and try again",True,C["red"]),(W//2-180,H//2+80))
            surf.blit(F_SM.render("Press ESC to go back",True,C["gray"]),(W//2-100,H//2+115))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN and ev.key==pygame.K_ESCAPE: return False
        if time.time()-start_t>10: return False
    return True

F_HUGE = pygame.font.SysFont("Trebuchet MS", 52, bold=True)
F_BIG  = pygame.font.SysFont("Trebuchet MS", 28, bold=True)
F_MED  = pygame.font.SysFont("Trebuchet MS", 18, bold=True)
F_SM   = pygame.font.SysFont("Trebuchet MS", 13, bold=True)

C = dict(
    bg=(6,4,10), white=(240,240,240), black=(6,6,6),
    red=(220,50,60), green=(50,200,80), blue=(60,140,220),
    yellow=(255,215,0), orange=(230,120,30), purple=(160,80,220),
    cyan=(60,220,200), gold=(255,200,50), pink=(255,80,180),
    teal=(40,200,180), lime=(140,220,40), gray=(120,120,130),
    brown=(100,70,40), dkblue=(30,60,120), silver=(200,200,220),
    crimson=(180,20,40), violet=(120,40,200), ember=(255,100,20),
)
RARITY_COL = {
    "common":    (180,180,180),
    "uncommon":  (80, 220, 80),
    "rare":      (80, 130, 255),
    "epic":      (180, 60, 220),
    "legendary": (255, 160, 0),
}

# ─── META SAVE ────────────────────────────────────────────────────────────────
SAVE_FILE = "soul_descent_meta.json"
META_UPGRADES = [
    dict(id="hp_up",     name="Iron Body",     desc="+15 Max HP per run",       cost=10, max_level=5),
    dict(id="dmg_up",    name="Sharp Edge",    desc="+8% damage per run",       cost=15, max_level=5),
    dict(id="crit_up",   name="Eagle Eye",     desc="+5% crit per run",         cost=12, max_level=4),
    dict(id="dash_up",   name="Wind Step",     desc="-0.1s dash CD per run",    cost=8,  max_level=3),
    dict(id="coin_mag",  name="Coin Magnet",   desc="Auto-collect nearby coins",cost=18, max_level=1),
    dict(id="revive",    name="Second Chance", desc="Revive once per run at 30HP",cost=30,max_level=1),
]
def load_meta():
    if os.path.exists(SAVE_FILE):
        try:
            with open(SAVE_FILE) as f: return json.load(f)
        except: pass
    return {"coins_total":0,"runs":0,"best_floor":0,"best_score":0,
            "upgrades":{u["id"]:0 for u in META_UPGRADES}}
def save_meta(m):
    try:
        with open(SAVE_FILE,"w") as f: json.dump(m,f,indent=2)
    except: pass
META = load_meta()

# ─── PROCEDURAL SFX ───────────────────────────────────────────────────────────
class SFX:
    _c = {}
    @staticmethod
    def _gen(freq, dur, shape="sine", vol=0.28, decay=True, vibrato=0, harmonics=None):
        try:
            import numpy as np
            sr = 44100; n = int(sr*dur)
            t  = np.linspace(0, dur, n, endpoint=False)
            if vibrato > 0:
                freq_mod = freq * (1 + vibrato * np.sin(2*np.pi*6*t))
            else:
                freq_mod = freq
            ph = 2*np.pi * np.cumsum(freq_mod) / sr if vibrato>0 else 2*np.pi*freq*t
            if shape=="sine":     w = np.sin(ph)
            elif shape=="square": w = np.sign(np.sin(ph))
            elif shape=="saw":    w = 2*(freq*t % 1) - 1
            elif shape=="tri":    w = 2*np.abs(2*(freq*t % 1)-1) - 1
            elif shape=="noise":  w = np.random.uniform(-1,1,n)
            else:                 w = np.sin(ph)
            if harmonics:
                for hf, ha in harmonics:
                    w += ha * np.sin(2*np.pi*hf*t)
            atk = int(n*0.02); rel = int(n*0.15) if decay else 0
            env = np.ones(n)
            if atk>0:  env[:atk]  = np.linspace(0,1,atk)
            if rel>0:  env[-rel:] = np.linspace(1,0,rel)
            if decay:  env *= np.exp(-t*2.5)
            s = np.clip(w*env*vol, -1, 1)
            a = (s*32767).astype(np.int16)
            stereo = np.column_stack([a,a])
            return pygame.sndarray.make_sound(stereo)
        except: return None

    @classmethod
    def play(cls, name):
        if not pygame.mixer.get_init(): return
        if name not in cls._c:
            s = None
            # ── Weapons ──
            if   name=="shoot":       s=cls._gen(880,0.08,"sine",0.18,harmonics=[(1760,0.08),(440,0.12)])
            elif name=="shoot_heavy": s=cls._gen(220,0.14,"saw",0.28,harmonics=[(110,0.15),(440,0.08)])
            elif name=="shotgun":     s=cls._gen(300,0.10,"noise",0.35,vibrato=0.1)
            elif name=="bow":         s=cls._gen(600,0.06,"tri",0.20)
            elif name=="laser":       s=cls._gen(1200,0.05,"sine",0.15,vibrato=0.05)
            elif name=="flame":       s=cls._gen(200,0.04,"noise",0.20)
            elif name=="magic":       s=cls._gen(660,0.10,"sine",0.22,vibrato=0.08,harmonics=[(990,0.12)])
            elif name=="melee":       s=cls._gen(180,0.09,"square",0.28,harmonics=[(360,0.10)])
            elif name=="melee_heavy": s=cls._gen(120,0.13,"square",0.35,harmonics=[(240,0.12)])
            # ── Combat ──
            elif name=="hit":         s=cls._gen(350,0.06,"noise",0.30)
            elif name=="crit":        s=cls._gen(1100,0.08,"sine",0.32,vibrato=0.04)
            elif name=="enemy_die":   s=cls._gen(180,0.18,"noise",0.32,harmonics=[(90,0.2)])
            elif name=="boss_hit":    s=cls._gen(100,0.12,"square",0.40,harmonics=[(50,0.25)])
            elif name=="boss_die":    s=cls._gen(60,0.55,"sine",0.42,vibrato=0.03,harmonics=[(120,0.2),(30,0.3)])
            elif name=="explode":     s=cls._gen(80,0.35,"noise",0.50,harmonics=[(40,0.3)])
            elif name=="burn":        s=cls._gen(240,0.05,"noise",0.14)
            elif name=="freeze":      s=cls._gen(1400,0.06,"sine",0.16,vibrato=0.06)
            # ── Movement ──
            elif name=="dash":        s=cls._gen(660,0.07,"sine",0.18,vibrato=0.05)
            elif name=="dash_full":   s=cls._gen(880,0.10,"sine",0.28,vibrato=0.08,harmonics=[(1320,0.15)])
            # ── UI ──
            elif name=="pickup":      s=cls._gen(880,0.07,"sine",0.22,decay=False,harmonics=[(1320,0.12)])
            elif name=="levelup":     s=cls._gen(660,0.22,"sine",0.35,decay=False,vibrato=0.03,harmonics=[(990,0.18),(1320,0.10)])
            elif name=="shop_buy":    s=cls._gen(550,0.14,"tri",0.28,decay=False,harmonics=[(825,0.12)])
            elif name=="shield":      s=cls._gen(440,0.12,"square",0.22,decay=False)
            elif name=="room_clear":  s=cls._gen(440,0.30,"sine",0.38,decay=False,vibrato=0.02,harmonics=[(660,0.20),(880,0.12)])
            elif name=="boss_spawn":  s=cls._gen(55,0.80,"sine",0.45,vibrato=0.02,harmonics=[(110,0.25),(165,0.15)])
            cls._c[name] = s
        snd = cls._c.get(name)
        if snd:
            try: snd.play()
            except: pass

# ─── MUSIC ENGINE ─────────────────────────────────────────────────────────────
class Music:
    SR=22050; BPM=92
    SCALE=[110,123.5,130.8,146.8,164.8,185,196,220,246.9,261.6,293.7,329.6]
    def __init__(self):
        self._normal=None; self._boss=None; self._ready=False; self._mode="none"
        threading.Thread(target=self._build,daemon=True).start()
    def _build(self):
        try:
            import numpy as np
        except: return
        sr=self.SR; bpm=self.BPM; beat=sr*60/bpm; bar=beat*4
        N=int(bar*8); t=np.linspace(0,N/sr,N,False)
        def osc(f,sh="sine",amp=1.0):
            ph=2*np.pi*f*t
            if sh=="sine": return amp*np.sin(ph)
            if sh=="saw":  return amp*(2*(f*t%1)-1)
            if sh=="tri":  return amp*(2*np.abs(2*(f*t%1)-1)-1)
            return amp*np.sin(ph)
        def env_note(start,dur,atk=0.01,rel=0.08):
            e=np.zeros(N); s=int(start); d=int(dur)
            a=int(atk*sr); r=min(int(rel*sr),d)
            seg=min(d,N-s)
            if seg<=0: return e
            e[s:s+min(a,seg)]=np.linspace(0,1,min(a,seg))
            if seg>a: e[s+a:s+seg]=1.0
            if seg>=r: e[s+seg-r:s+seg]*=np.linspace(1,0,r)
            return e
        # Bass
        bass=np.zeros(N)
        bass_seq=[0,0,2,0,4,0,2,3]
        bstep=int(beat)
        for i,ni in enumerate(bass_seq*2):
            f=self.SCALE[ni]*0.5
            e=env_note(i*bstep,bstep*0.9)
            bass+=osc(f,"saw",0.22)*e+osc(f*2,"sine",0.08)*e
        # Pad
        pad=np.zeros(N)
        for ni in [0,4,7,11]:
            f=self.SCALE[ni]
            pad+=osc(f,"sine",0.06)+osc(f*2,"sine",0.03)
        pad*=0.7+0.3*np.sin(2*np.pi*0.5*t)
        # Melody
        mel=np.zeros(N)
        pat=[7,9,11,9,7,4,5,7,9,11,12,11,9,7,5,4]
        mstep=int(beat*0.5)
        for i,ni in enumerate(pat[:N//mstep]):
            f=self.SCALE[min(ni,len(self.SCALE)-1)]
            e=env_note(i*mstep,int(mstep*0.7),0.005,0.05)
            mel+=osc(f,"tri",0.14)*e
        # Hi-hat
        perc=np.zeros(N)
        hh=int(beat*0.25)
        for i in range(N//hh):
            dur=int(sr*0.018); s=i*hh
            if s+dur>N: break
            noise=np.random.uniform(-1,1,dur)
            noise*=np.exp(-np.linspace(0,1,dur)*180)*0.10
            perc[s:s+dur]+=noise
        # Kick
        for i in range(16):
            if i%4==0:
                s=int(i*beat); dur=int(sr*0.18)
                if s+dur>N: break
                nt=np.linspace(0,0.18,dur)
                k=np.sin(2*np.pi*(90-75*nt/0.18)*nt)*np.exp(-nt*22)*0.45
                perc[s:s+dur]+=k
            if i%4==2:
                s=int(i*beat); dur=int(sr*0.06)
                if s+dur>N: break
                noise=np.random.uniform(-1,1,dur)*np.exp(-np.linspace(0,1,dur)*50)*0.28
                perc[s:s+dur]+=noise
        mix=bass+pad+mel+perc
        mx=np.max(np.abs(mix)); mix=mix/mx*0.22 if mx>0 else mix
        # Boss: add driving pulse + distorted bass
        pulse=np.zeros(N)
        ps=int(beat*0.25)
        for i in range(N//ps):
            s2=i*ps; dur2=int(sr*0.04)
            if s2+dur2>N: break
            nt2=np.linspace(0,0.04,dur2)
            pulse[s2:s2+dur2]+=np.sin(2*np.pi*110*nt2)*np.exp(-nt2*80)*0.35
        dist_bass=np.clip(bass*3,-0.8,0.8)*0.3
        boss_mix=mix*0.6+pulse*0.3+dist_bass*0.1
        mx2=np.max(np.abs(boss_mix)); boss_mix=boss_mix/mx2*0.30 if mx2>0 else boss_mix
        def to_snd(arr):
            a16=(arr*32767).astype(np.int16)
            return pygame.sndarray.make_sound(np.column_stack([a16,a16]))
        try:
            self._normal=to_snd(mix); self._boss=to_snd(boss_mix); self._ready=True
        except: pass
    def start(self):
        if self._ready and self._normal:
            try: self._normal.play(-1); self._mode="normal"
            except: pass
    def set_mode(self,mode):
        if mode==self._mode or not self._ready: return
        self._mode=mode
        try:
            pygame.mixer.fadeout(600)
            pygame.time.set_timer(pygame.USEREVENT+1,650)
        except: pass
        self._pending=mode
    def try_start_pending(self):
        if hasattr(self,"_pending") and self._pending:
            m=self._pending; self._pending=None
            try:
                if m=="boss" and self._boss: self._boss.play(-1)
                elif m=="normal" and self._normal: self._normal.play(-1)
            except: pass
    def stop(self):
        try: pygame.mixer.stop()
        except: pass
    def is_ready(self): return self._ready

MUSIC = Music()

# ─── TILE TEXTURES ────────────────────────────────────────────────────────────
def _make_tiles():
    rng=random.Random(99)
    # 3 FLOOR THEMES: stone, cave, lava
    themes = [
        dict(base=(28,22,34), var=16, crack=(16,12,20), edge=(18,14,24)),   # stone
        dict(base=(22,30,28), var=14, crack=(14,20,18), edge=(14,22,18)),   # cave
        dict(base=(38,18,14), var=18, crack=(28,12,10), edge=(26,14,12)),   # lava
    ]
    all_variants = []
    for theme in themes:
        for _ in range(4):
            t=pygame.Surface((TILE,TILE)); t.fill(theme["base"])
            for _ in range(18):
                c=tuple(max(0,min(255,theme["base"][i]+rng.randint(-theme["var"],theme["var"]))) for i in range(3))
                pygame.draw.rect(t,c,(rng.randint(0,TILE-5),rng.randint(0,TILE-5),rng.randint(3,10),rng.randint(2,5)))
            if rng.random()<.45:
                cx2,cy2=rng.randint(5,TILE-5),rng.randint(5,TILE-5)
                pygame.draw.line(t,theme["crack"],(cx2,cy2),(cx2+rng.randint(-10,10),cy2+rng.randint(-10,10)),1)
            if rng.random()<.2:
                for _ in range(3):
                    pygame.draw.circle(t,theme["crack"],(rng.randint(4,TILE-4),rng.randint(4,TILE-4)),rng.randint(1,3))
            pygame.draw.rect(t,theme["edge"],(0,0,TILE,TILE),1)
            all_variants.append(t)

    # Wall fronts (3 themes)
    wall_fronts=[]; wall_tops=[]
    wall_themes=[
        dict(base=(48,38,55),  mortar=(26,18,32), top=(68,56,78),  topline=(92,78,108)),
        dict(base=(32,46,42),  mortar=(18,28,24), top=(48,64,58),  topline=(70,88,80)),
        dict(base=(60,30,24),  mortar=(38,18,14), top=(80,45,36),  topline=(100,62,50)),
    ]
    for wt in wall_themes:
        wf=pygame.Surface((TILE,TILE)); wf.fill(wt["base"])
        stones=[(0,0,19,19),(21,0,19,19),(10,21,19,19),(0,21,9,19),(31,21,9,19)]
        for bx,by,bw,bh in stones:
            c=tuple(max(0,min(255,wt["base"][i]+rng.randint(-12,12))) for i in range(3))
            pygame.draw.rect(wf,c,(bx+1,by+1,bw-1,bh-1))
            pygame.draw.line(wf,(min(255,wt["base"][0]+20),min(255,wt["base"][1]+16),min(255,wt["base"][2]+24)),
                             (bx+1,by+1),(bx+bw-1,by+1),1)
            pygame.draw.rect(wf,wt["mortar"],(bx,by,bw+1,bh+1),1)
        wall_fronts.append(wf)
        wto=pygame.Surface((TILE,16)); wto.fill(wt["top"])
        for i in range(0,TILE,8):
            shade=rng.randint(-8,8)
            c2=tuple(max(0,min(255,wt["top"][j]+shade)) for j in range(3))
            pygame.draw.rect(wto,c2,(i,2,7,12))
        pygame.draw.line(wto,wt["topline"],(0,0),(TILE,0),2)
        pygame.draw.line(wto,(wt["top"][0]-15,wt["top"][1]-12,wt["top"][2]-18),(0,15),(TILE,15),1)
        wall_tops.append(wto)
    return all_variants, wall_fronts, wall_tops

FLOOR_V, WALL_FRONTS, WALL_TOPS = _make_tiles()

def _make_light(r,col,a=255):
    s=pygame.Surface((r*2,r*2),pygame.SRCALPHA)
    for i in range(r,0,-3):
        al=int((1-(i/r)**1.8)*a)
        pygame.draw.circle(s,(*col,al),(r,r),i)
    return s

LT_PLAYER=_make_light(230,(180,210,255),115)
LT_TORCH =_make_light(270,(255,140,40),130)
LT_PROJ  =_make_light(100,C["purple"],190)
LT_BOSS  =_make_light(320,C["red"],85)
LT_LAVA  =_make_light(200,(255,80,20),100)

def _make_vignette():
    s=pygame.Surface((W,H),pygame.SRCALPHA)
    cx,cy=W//2,H//2
    for i in range(30,0,-1):
        ratio=i/30
        if ratio<0.45: continue
        a=int(((ratio-0.45)/0.55)**2.0*180)
        rw=int(cx*ratio*2.2); rh=int(cy*ratio*2.2)
        pygame.draw.ellipse(s,(0,0,0,a),(cx-rw//2,cy-rh//2,rw,rh),12)
    return s
VIGNETTE=_make_vignette()
COLOR_GRADE=pygame.Surface((W,H),pygame.SRCALPHA); COLOR_GRADE.fill((35,18,55,16))

# ─── PIXEL ART SPRITES ────────────────────────────────────────────────────────
def _px(surf,pmap,cols,ox,oy,sc=3):
    for row,line in enumerate(pmap):
        for col,ch in enumerate(line):
            if ch=='.': continue
            pygame.draw.rect(surf,cols.get(ch,(255,0,255)),(ox+col*sc,oy+row*sc,sc,sc))

def _spr(pmap,cols,sc=3):
    s=pygame.Surface((len(pmap[0])*sc,len(pmap)*sc),pygame.SRCALPHA)
    _px(s,pmap,cols,0,0,sc); return s

PLAYER_PIX=["....HHHH....","...HSSSBH...","...HSSSBH...","..BBSSSSBB..","..BASSSSAB..","..BAOOOOAB..","..BASSSSAB..","...BBBBBBB..","...LLLLLL...","..LSSSSSSL..","..LSSSSSSL..",".WLS....SLW.",".WBB....BBW."]
PLAYER_COL={'H':(50,50,70),'S':(200,170,130),'B':(50,80,160),'A':(70,110,200),'O':(15,15,20),'L':(40,60,140),'W':(160,160,170)}
GOBLIN_PIX=["..GGGG..","GEEEEG.","GOEOEG.","GEEEEG.","..GMMG..","GGGGGG.","BB..BB.","BB..BB."]
GOBLIN_COL={'G':(40,90,40),'E':(100,160,80),'O':(10,10,10),'M':(55,120,45),'B':(70,45,25)}
ORC_PIX=["...OOOO...","..OBBBBO..","..OBWWBO..","..OBBBBO..","..OOOOOO..","OAAAAAO..","OAAAAAO..","..OAAAOO..","..OO..OO..","..OO..OO.."]
ORC_COL={'O':(130,75,20),'B':(180,120,60),'W':(15,15,15),'A':(100,65,25)}
MAGE_PIX=["..PPPP..","PSSSSP.","PSOSOSP","PSSSSP.","..MMMM..","RRRRRR.","RSSSRR.","RSSSRR.","..RR.RR."]
MAGE_COL={'P':(80,30,130),'S':(220,190,200),'O':(15,15,15),'M':(55,15,90),'R':(120,50,180)}
ARCHER_PIX=["..AAAA..","ASSSAS.","ASOSOA.","ASSSAS.","..AAMM..","TTTTT..","TSSST..","TSSST..","..TT.T.."]
ARCHER_COL={'A':(30,100,30),'S':(200,170,130),'O':(10,10,10),'M':(20,70,20),'T':(60,40,20)}
ELITE_PIX=["...EEEE...","..EBBBBE..","..EBWWBE..","..EBBBBE..","..EEEEEE..","EAAAAAEE.","EAAAAAEE.","..EAAAEE..","..EE..EE..","..EE..EE.."]
ELITE_COL={'E':(200,100,0),'B':(255,160,50),'W':(15,15,15),'A':(180,80,0)}

SPRITE_PLAYER=_spr(PLAYER_PIX,PLAYER_COL,3)
SPRITE_GOBLIN=_spr(GOBLIN_PIX,GOBLIN_COL,3)
SPRITE_ORC   =_spr(ORC_PIX,ORC_COL,3)
SPRITE_MAGE  =_spr(MAGE_PIX,MAGE_COL,3)
SPRITE_ARCHER=_spr(ARCHER_PIX,ARCHER_COL,3)
SPRITE_ELITE =_spr(ELITE_PIX,ELITE_COL,3)

# ─── ANIMATED PIXEL ART SPRITES ──────────────────────────────────────────────
# Each sprite has multiple frames: [idle_frame, walk_frame1, walk_frame2, attack_frame]

# Player frames (12-col × 13-row, scale 3)
_P = {
    'H':(50,50,70),'S':(200,170,130),'B':(50,80,160),'A':(70,110,200),
    'O':(15,15,20),'L':(40,60,140),'W':(160,160,170),'G':(80,120,220),
    'R':(220,60,60),'Y':(240,210,60),
}
_PLAYER_FRAMES = [
    # Frame 0 – idle (arms relaxed)
    ["....HHHH....","...HSSSBH...","...HSSSBH...","..BBSSSSBB..",
     "..BASSSSAB..","..BAOOOOAB..","..BASSSSAB..","...BBBBBBB..",
     "...LLLLLL...","..LSSSSSSL..","..LSSSSSSL..",".WLS....SLW.",".WBB....BBW."],
    # Frame 1 – walk A (left leg forward)
    ["....HHHH....","...HSSSBH...","...HSSSBH...","..BBSSSSBB..",
     "..BASSSSAB..","..BAOOOOAB..","..BASSSSAB..","...BBBBBBB..",
     "...LLLLLL...","..LSSSSSSL..","..LSSSSSSL..",".WLS....SL..","..BB....WBW."],
    # Frame 2 – walk B (right leg forward)
    ["....HHHH....","...HSSSBH...","...HSSSBH...","..BBSSSSBB..",
     "..BASSSSAB..","..BAOOOOAB..","..BASSSSAB..","...BBBBBBB..",
     "...LLLLLL...","..LSSSSSSL..","..LSSSSSSL..","..LS....SLW.",".WBW....BB.."],
    # Frame 3 – attack (leaning forward)
    ["....HHHH....","...HSSSBH...","..HSSSBH....","..BBSSSSBB..",
     ".BASSSSABB..","..BAOOOOAB..","..BASSSSAB..","...BBBBBBB..",
     "...LLLLLL...","..LSSSSSSL..","..LSSSSSSL..",".WLS....SLW.",".WBB....BBW."],
]
_GOBLIN_FRAMES = [
    # idle
    ["..GGGG..","GEEEEG.","GOEOEG.","GEEEEG.","..GMMG..","GGGGGG.","BB..BB.","BB..BB."],
    # walk A
    ["..GGGG..","GEEEEG.","GOEOEG.","GEEEEG.","..GMMG..","GGGGGG.",".B..BB.","BB..B.."],
    # walk B
    ["..GGGG..","GEEEEG.","GOEOEG.","GEEEEG.","..GMMG..","GGGGGG.","BB..B..",".B..BB."],
    # attack
    ["..GGGG..",".GEEEEG","GGOEOEG","GEEEEG.","..GMMG..","GGGGGG.","BB..BB.","BB..BB."],
]
_ORC_FRAMES = [
    ["...OOOO...","..OBBBBO..","..OBWWBO..","..OBBBBO..","..OOOOOO..","OAAAAAO..","OAAAAAO..","..OAAAOO..","..OO..OO..","..OO..OO.."],
    ["...OOOO...","..OBBBBO..","..OBWWBO..","..OBBBBO..","..OOOOOO..","OAAAAAO..","OAAAAAO..","..OAAAOO..","..OO..OO..",".OO...OO.."],
    ["...OOOO...","..OBBBBO..","..OBWWBO..","..OBBBBO..","..OOOOOO..","OAAAAAO..","OAAAAAO..","..OAAAOO..",".OO...OO..","..OO..OO.."],
    ["..OOOO....","..OBBBBO..","..OBWWBO..","..OBBBBO..","..OOOOOO..","OAAAAAO..","OAAAAAO..","..OAAAOO..","..OO..OO..","..OO..OO.."],
]
_MAGE_FRAMES = [
    ["..PPPP..","PSSSSP.","PSOSOSP","PSSSSP.","..MMMM..","RRRRRR.","RSSSRR.","RSSSRR.","..RR.RR."],
    ["..PPPP..","PSSSSP.","PSOSOSP","PSSSSP.","..MMMM..","RRRRRR.","RSSSRR.","RSSSRR.",".RR..RR."],
    ["..PPPP..","PSSSSP.","PSOSOSP","PSSSSP.","..MMMM..","RRRRRR.","RSSSRR.","RSSSRR.","RR...RR."],
    [".PPPP...","PSSSSP.","PSOSOPP","PSSSSP.","..MMMM..","RRRRRR.","RSSSRR.","RSSSRR.","..RR.RR."],
]

def _make_anim_sprites(frames, colors, sc=3):
    """Build list of surfaces, one per frame."""
    return [_spr(f, colors, sc) for f in frames]

ANIM_PLAYER = _make_anim_sprites(_PLAYER_FRAMES, _P, 3)
ANIM_GOBLIN = _make_anim_sprites(_GOBLIN_FRAMES, GOBLIN_COL, 3)
ANIM_ORC    = _make_anim_sprites(_ORC_FRAMES,    ORC_COL,    3)
ANIM_MAGE   = _make_anim_sprites(_MAGE_FRAMES,   MAGE_COL,   3)

# Keep originals for enemies without anim
SPRITE_PLAYER = ANIM_PLAYER[0]
SPRITE_GOBLIN = ANIM_GOBLIN[0]
SPRITE_ORC    = ANIM_ORC[0]
SPRITE_MAGE   = ANIM_MAGE[0]
SPRITE_ARCHER = _spr(ARCHER_PIX, ARCHER_COL, 3)
SPRITE_ELITE  = _spr(ELITE_PIX,  ELITE_COL,  3)

# Per-class player sprites (tinted versions of base frames)
def _tint_frames(frames, tint_col, strength=120):
    """Return copies of frames tinted with a color."""
    result = []
    for frame in frames:
        s = frame.copy()
        t2 = pygame.Surface(s.get_size(), pygame.SRCALPHA)
        t2.fill((*tint_col, strength))
        s.blit(t2, (0,0), special_flags=pygame.BLEND_RGBA_ADD)
        result.append(s)
    return result

# Warrior: orange-red tint, bigger sprite (sc=3)
_WARRIOR_COL = {**_P, 'B':(160,60,10),'A':(200,80,20),'L':(140,50,10),'W':(180,100,40)}
ANIM_WARRIOR = _make_anim_sprites(_PLAYER_FRAMES, _WARRIOR_COL, 3)

# Mage: purple tint, slightly smaller look
_MAGE_P_COL = {**_P, 'B':(100,30,180),'A':(130,50,210),'L':(80,20,150),'W':(160,120,220),'S':(220,190,240)}
ANIM_MAGE_P = _make_anim_sprites(_PLAYER_FRAMES, _MAGE_P_COL, 3)

# Rogue: cyan/dark tint
_ROGUE_COL = {**_P, 'B':(20,100,120),'A':(30,140,160),'L':(15,80,100),'W':(80,200,190),'S':(190,220,220)}
ANIM_ROGUE = _make_anim_sprites(_PLAYER_FRAMES, _ROGUE_COL, 3)

CLASS_ANIMS = {"Warrior": ANIM_WARRIOR, "Mage": ANIM_MAGE_P, "Rogue": ANIM_ROGUE}

# Class aura colors for shield/highlight
CLASS_AURA = {"Warrior": C["orange"], "Mage": C["purple"], "Rogue": C["cyan"]}

WALK_FPS = 8   # animation frames per second

def get_anim_frame(anim_list, t, vel, swinging=False, attack_t=0):
    """Pick the right animation frame."""
    spd = math.hypot(vel.x, vel.y) if hasattr(vel,'x') else 0
    if swinging or attack_t > 0: return anim_list[3] if len(anim_list)>3 else anim_list[0]
    if spd > 30:
        idx = int(t * WALK_FPS) % 2 + 1   # alternate frame 1 and 2
        return anim_list[idx] if len(anim_list)>idx else anim_list[0]
    return anim_list[0]

def draw_sprite(surf, sprite, cx, cy, flip_x=False, tint=None, alpha=255):
    s = pygame.transform.flip(sprite, flip_x, False) if flip_x else sprite
    if tint or alpha < 255:
        s = s.copy()
        if alpha < 255: s.set_alpha(alpha)
        if tint:
            t2 = pygame.Surface(s.get_size(), pygame.SRCALPHA)
            t2.fill((*tint, 80))
            s.blit(t2, (0,0), special_flags=pygame.BLEND_RGBA_ADD)
    surf.blit(s, (int(cx - s.get_width()//2), int(cy - s.get_height()//2)))

# ─── ENVIRONMENT PARTICLE SYSTEM ──────────────────────────────────────────────
class EnvFX:
    """Ambient environment particles: rain drops, dust, ember sparks, cave drips."""
    def __init__(self):
        self.particles = []  # [x,y,vx,vy,life,max_life,col,sz,kind]
        self._spawn_t = 0.0

    def set_theme(self, theme):
        self._theme = theme
        self.particles.clear()

    def update(self, dt, cam, grid):
        self._spawn_t += dt
        theme = getattr(self, '_theme', 0)
        # Spawn rate
        rate = 0.02  # seconds between spawns
        while self._spawn_t >= rate:
            self._spawn_t -= rate
            # Spawn in visible area around camera
            cx = cam.pos.x + random.uniform(-W//2, W//2)
            cy = cam.pos.y + random.uniform(-H//2, H//2)
            if theme == 0:  # stone – floating dust motes
                self.particles.append([cx, cy-random.uniform(0,200),
                    random.uniform(-6,6), random.uniform(4,18),
                    random.uniform(1.5,3.5), 3.5, (140,130,155), random.randint(1,3), 'dust'])
            elif theme == 1:  # cave – dripping water
                self.particles.append([cx, cy-random.uniform(0,300),
                    random.uniform(-2,2), random.uniform(60,140),
                    random.uniform(.8,1.8), 1.8, (80,160,200), 2, 'drop'])
            else:  # lava – ember sparks
                self.particles.append([cx, cy+random.uniform(0,100),
                    random.uniform(-25,25), random.uniform(-80,-20),
                    random.uniform(.5,1.5), 1.5, random.choice([(255,100,20),(255,180,40),(220,60,10)]), random.randint(2,4), 'ember'])

        for p in self.particles:
            p[0]+=p[2]*dt; p[1]+=p[3]*dt; p[4]-=dt
            if p[8]=='ember': p[3]+=40*dt  # gravity on embers
            if p[8]=='dust': p[3]=max(p[3]-8*dt,2)  # slow drift
        self.particles=[p for p in self.particles if p[4]>0 and len(self.particles)<200]

    def draw(self, surf, cam):
        for p in self.particles:
            pos=cam.apply(Vector2(p[0],p[1]))
            if not(-5<pos.x<W+5 and -5<pos.y<H+5): continue
            a=max(0,min(255,int(p[4]/p[5]*180)))
            col=p[6]; sz=max(1,int(p[7]))   # p[6]=col, p[7]=sz
            if p[8]=='drop':
                pygame.draw.line(surf,(*col,a),(int(pos.x),int(pos.y)),(int(pos.x),int(pos.y)+4),1)
            else:
                s2=pygame.Surface((sz*2,sz*2),pygame.SRCALPHA)
                pygame.draw.circle(s2,(*col,a),(sz,sz),sz)
                surf.blit(s2,(int(pos.x-sz),int(pos.y-sz)))

# ─── WEAPON TRAIL SYSTEM ──────────────────────────────────────────────────────
class WeaponTrail:
    """Per-projectile color trail based on weapon type."""
    # trail_col, width, length(n points), glow
    STYLES = {
        "melee":        dict(col=(220,220,240), w=3, glow=False),
        "Pistol":       dict(col=(200,200,200), w=2, glow=False),
        "Shotgun":      dict(col=(230,130,40),  w=2, glow=False),
        "Bow":          dict(col=(140,220,60),  w=2, glow=True),
        "Magic Orb":    dict(col=(180,80,240),  w=3, glow=True),
        "Laser Rifle":  dict(col=(40,240,220),  w=4, glow=True),
        "Grenade":      dict(col=(120,200,50),  w=3, glow=False),
        "Flamethrower": dict(col=(255,100,20),  w=3, glow=True),
        "Lightning":    dict(col=(255,220,40),  w=3, glow=True),
        "Plasma Canon": dict(col=(255,80,200),  w=4, glow=True),
        "Void Blade":   dict(col=(160,60,240),  w=4, glow=True),
        "Soul Reaper":  dict(col=(220,40,60),   w=4, glow=True),
    }

    def __init__(self):
        # wkey -> list of (pos, alpha) trail points
        self._trails = {}

    def add(self, wkey, pos):
        if wkey not in self._trails: self._trails[wkey] = []
        self._trails[wkey].append([Vector2(pos), 255])

    def update(self, dt):
        for key in list(self._trails.keys()):
            trail = self._trails[key]
            for pt in trail: pt[1] -= dt * 600
            self._trails[key] = [pt for pt in trail if pt[1] > 0]

    def draw_for_proj(self, surf, cam, proj_pos, wkey, col):
        style = self.STYLES.get(wkey, dict(col=col, w=2, glow=False))
        trail_col = style['col']
        w = style['w']
        glow = style['glow']
        # Draw fading line from slightly behind proj
        p = cam.apply(proj_pos)
        # Glow ring
        if glow:
            gs = pygame.Surface((20,20), pygame.SRCALPHA)
            pygame.draw.circle(gs, (*trail_col, 50), (10,10), 9)
            surf.blit(gs, (int(p.x-10), int(p.y-10)))
        # Inner bright dot
        pygame.draw.circle(surf, trail_col, (int(p.x),int(p.y)), w+1)
        pygame.draw.circle(surf, (255,255,255), (int(p.x),int(p.y)), max(1,w-1))

WEAPON_TRAIL = WeaponTrail()

# ─── HUD HELPERS ──────────────────────────────────────────────────────────────
def _draw_hud_panel(surf, x, y, w, h, alpha=200):
    """Dark rounded panel for HUD elements."""
    s = pygame.Surface((w,h), pygame.SRCALPHA)
    pygame.draw.rect(s, (8,6,18,alpha), (0,0,w,h), border_radius=8)
    pygame.draw.rect(s, (60,50,80,100), (0,0,w,h), 1, border_radius=8)
    surf.blit(s, (x,y))

def _draw_bar(surf, x, y, w, h, ratio, fg_col, bg_col=(20,15,28), border=True, animated_shine=False, t=0):
    """Polished bar with optional animated shine."""
    pygame.draw.rect(surf, bg_col, (x,y,w,h), border_radius=h//2)
    fw = int(w * max(0,min(1,ratio)))
    if fw > 0:
        pygame.draw.rect(surf, fg_col, (x,y,fw,h), border_radius=h//2)
        if animated_shine and fw > 10:
            shine_x = x + int((math.sin(t*3)+1)/2 * max(0,fw-10))
            ss = pygame.Surface((10,h), pygame.SRCALPHA)
            for i in range(10): pygame.draw.line(ss,(255,255,255,int(40*(1-abs(i-5)/5))),(i,0),(i,h))
            surf.blit(ss,(shine_x,y))
    if border: pygame.draw.rect(surf,(80,70,100),(x,y,w,h),1,border_radius=h//2)

def _draw_icon(surf, x, y, kind, col, size=16):
    """Draw simple icon for HUD."""
    cx,cy=x+size//2,y+size//2
    if kind=="hp":
        # Heart
        pygame.draw.circle(surf,col,(cx-4,cy-2),5)
        pygame.draw.circle(surf,col,(cx+4,cy-2),5)
        pygame.draw.polygon(surf,col,[(cx-8,cy),(cx,cy+8),(cx+8,cy)])
    elif kind=="xp":
        # Star
        for i in range(5):
            ang=math.radians(i*72-90); ang2=math.radians(i*72-90+36)
            pygame.draw.line(surf,col,(int(cx+math.cos(ang)*7),int(cy+math.sin(ang)*7)),(int(cx+math.cos(ang2)*3),int(cy+math.sin(ang2)*3)),2)
    elif kind=="ammo":
        # Bullet shape
        pygame.draw.rect(surf,col,(cx-2,cy-6,4,8),border_radius=2)
        pygame.draw.ellipse(surf,col,(cx-2,cy-8,4,4))
    elif kind=="dash":
        # Lightning bolt
        pts=[(cx,cy-7),(cx-3,cy+1),(cx+1,cy+1),(cx,cy+7),(cx+3,cy-1),(cx-1,cy-1)]
        pygame.draw.polygon(surf,col,pts)
    elif kind=="sword":
        pygame.draw.line(surf,col,(cx-6,cy+6),(cx+6,cy-6),3)
        pygame.draw.polygon(surf,col,[(cx+4,cy-8),(cx+8,cy-4),(cx+6,cy-6)])
    elif kind=="shield":
        pygame.draw.polygon(surf,col,[(cx,cy-7),(cx+7,cy-2),(cx+7,cy+4),(cx,cy+8),(cx-7,cy+4),(cx-7,cy-2)])
    elif kind=="coin":
        pygame.draw.circle(surf,(255,200,50),(cx,cy),6)
        pygame.draw.circle(surf,(255,160,0),(cx,cy),6,1)

# ─── WEAPONS ──────────────────────────────────────────────────────────────────
WEAPONS={
    "Rusty Sword": dict(cls="melee", dmg=12,spd=350,rng=62, arc=95, col=C["gray"],  rare="common",   ammo=-1,pspd=0,  spread=0, pellets=1,pierce=False,explosive=False,homing=False,sfx="melee"),
    "Iron Axe":    dict(cls="melee", dmg=22,spd=550,rng=58, arc=130,col=C["orange"],rare="uncommon", ammo=-1,pspd=0,  spread=0, pellets=1,pierce=False,explosive=False,homing=False,sfx="melee_heavy"),
    "Spear":       dict(cls="melee", dmg=16,spd=300,rng=90, arc=50, col=C["teal"],  rare="uncommon", ammo=-1,pspd=0,  spread=0, pellets=1,pierce=True, explosive=False,homing=False,sfx="melee"),
    "Void Blade":  dict(cls="melee", dmg=32,spd=400,rng=78, arc=160,col=C["purple"],rare="epic",     ammo=-1,pspd=0,  spread=0, pellets=1,pierce=False,explosive=False,homing=False,sfx="melee_heavy"),
    "Soul Reaper": dict(cls="melee", dmg=45,spd=480,rng=85, arc=210,col=C["red"],   rare="legendary",ammo=-1,pspd=0,  spread=0, pellets=1,pierce=True, explosive=False,homing=False,sfx="melee_heavy"),
    "Pistol":      dict(cls="ranged",dmg=14,spd=300,rng=0,  arc=0,  col=C["gray"],  rare="common",   ammo=12,pspd=520,spread=4, pellets=1,pierce=False,explosive=False,homing=False,sfx="shoot"),
    "Shotgun":     dict(cls="ranged",dmg=10,spd=600,rng=0,  arc=0,  col=C["orange"],rare="uncommon", ammo=6, pspd=420,spread=22,pellets=5,pierce=False,explosive=False,homing=False,sfx="shotgun"),
    "Bow":         dict(cls="ranged",dmg=25,spd=500,rng=0,  arc=0,  col=C["lime"],  rare="uncommon", ammo=15,pspd=580,spread=2, pellets=1,pierce=True, explosive=False,homing=False,sfx="bow"),
    "Magic Orb":   dict(cls="ranged",dmg=20,spd=400,rng=0,  arc=0,  col=C["purple"],rare="rare",     ammo=20,pspd=340,spread=8, pellets=3,pierce=False,explosive=False,homing=True, sfx="magic"),
    "Laser Rifle": dict(cls="laser", dmg=6, spd=80, rng=0,  arc=0,  col=C["cyan"],  rare="rare",     ammo=30,pspd=0,  spread=0, pellets=1,pierce=True, explosive=False,homing=False,sfx="laser"),
    "Grenade":     dict(cls="ranged",dmg=55,spd=900,rng=0,  arc=0,  col=C["lime"],  rare="rare",     ammo=5, pspd=300,spread=5, pellets=1,pierce=False,explosive=True, homing=False,sfx="shoot_heavy"),
    "Flamethrower":dict(cls="flame", dmg=4, spd=80, rng=0,  arc=0,  col=C["red"],   rare="epic",     ammo=40,pspd=220,spread=25,pellets=6,pierce=False,explosive=False,homing=False,sfx="flame"),
    "Lightning":   dict(cls="ranged",dmg=35,spd=200,rng=0,  arc=0,  col=C["yellow"],rare="epic",     ammo=10,pspd=600,spread=0, pellets=1,pierce=True, explosive=False,homing=True, sfx="magic"),
    "Plasma Canon":dict(cls="ranged",dmg=80,spd=1200,rng=0, arc=0,  col=C["pink"],  rare="legendary",ammo=8, pspd=480,spread=0, pellets=1,pierce=True, explosive=True, homing=False,sfx="shoot_heavy"),
}
MAX_AMMO={k:max(1,v["ammo"]) for k,v in WEAPONS.items() if v["ammo"]>0}

# ─── STATUS EFFECTS ───────────────────────────────────────────────────────────
STATUS_DEFS={
    "burn":  dict(col=(255,120,20),tick_dmg=3, tick_rate=0.4,duration=3.0),
    "freeze":dict(col=(100,200,255),tick_dmg=0,tick_rate=0,  duration=1.5),
    "poison":dict(col=(80,220,80),  tick_dmg=2,tick_rate=0.6,duration=4.0),
    "stun":  dict(col=(255,220,50), tick_dmg=0,tick_rate=0,  duration=0.6),
}
class StatusEffect:
    def __init__(self,kind):
        d=STATUS_DEFS[kind]; self.kind=kind; self.col=d["col"]
        self.tick_dmg=d["tick_dmg"]; self.tick_rate=d["tick_rate"]
        self.remaining=d["duration"]; self.tick_t=0.0
    def update(self,dt,ent,ps):
        self.remaining-=dt
        if self.tick_dmg>0:
            self.tick_t+=dt
            if self.tick_t>=self.tick_rate:
                self.tick_t=0; ent.hp-=self.tick_dmg
                ps.emit(ent.pos.x,ent.pos.y,3,self.col,60,.2,2)
        return self.remaining>0
    def draw_icon(self,surf,px,py,idx):
        ix=px-10+idx*14; iy=py-4
        pygame.draw.circle(surf,self.col,(ix,iy),5)
        pygame.draw.circle(surf,C["black"],(ix,iy),5,1)

# ─── RELICS ───────────────────────────────────────────────────────────────────
RELIC_DEFS=[
    dict(id="iron_heart",  name="Iron Heart",   desc="+20 MaxHP, regen 1HP/5s",     col=C["red"],   rare="uncommon"),
    dict(id="bloodstone",  name="Bloodstone",   desc="5% lifesteal on hit",         col=(180,20,20),rare="rare"),
    dict(id="shadow_cloak",name="Shadow Cloak", desc="Dash invuln+0.15s",           col=C["purple"],rare="rare"),
    dict(id="berserker",   name="Berserker",    desc="+40% dmg when HP<30%",        col=C["orange"],rare="epic"),
    dict(id="frost_touch", name="Frost Touch",  desc="Melee hits apply Freeze",     col=C["cyan"],  rare="rare"),
    dict(id="inferno",     name="Inferno",      desc="Ranged hits apply Burn",      col=C["red"],   rare="epic"),
    dict(id="toxic_rounds",name="Toxic Rounds", desc="All hits apply Poison",       col=C["lime"],  rare="rare"),
    dict(id="glass_cannon",name="Glass Cannon", desc="+60% dmg, -30 MaxHP",        col=C["pink"],  rare="legendary"),
    dict(id="vampiric",    name="Vampiric",     desc="Kill restores 5HP",           col=(200,0,50), rare="epic"),
    dict(id="swift_boots", name="Swift Boots",  desc="Move speed +25%",             col=C["lime"],  rare="uncommon"),
    dict(id="bomb_belt",   name="Bomb Belt",    desc="20% explode on kill",         col=C["orange"],rare="epic"),
    dict(id="soul_link",   name="Soul Link",    desc="XP = bonus coins on kill",    col=C["gold"],  rare="rare"),
    dict(id="time_warp",   name="Time Warp",    desc="Crit hit triggers 0.2s slow", col=C["teal"],  rare="epic"),
    dict(id="phoenix",     name="Phoenix",      desc="Revive once at full HP",      col=(255,140,0),rare="legendary"),
]
RELIC_BY_ID={r["id"]:r for r in RELIC_DEFS}

# ─── SHOP ─────────────────────────────────────────────────────────────────────
SHOP_ITEMS=[
    dict(kind="hp_full", name="Full Heal",     desc="Restore all HP",        cost=8,  col=C["red"]),
    dict(kind="hp_half", name="Half Heal",     desc="Restore 50% HP",        cost=4,  col=C["red"]),
    dict(kind="ammo",    name="Full Ammo",     desc="Reload all weapons",    cost=5,  col=C["gray"]),
    dict(kind="relic",   name="Mystery Relic", desc="Random relic",          cost=15, col=C["gold"]),
    dict(kind="weapon",  name="Rare Weapon",   desc="Pick a rare weapon",    cost=20, col=C["purple"]),
    dict(kind="crit",    name="Scope",         desc="+8% crit chance",       cost=10, col=C["cyan"]),
    dict(kind="max_hp",  name="Heart+25",      desc="+25 Max HP",            cost=12, col=C["red"]),
    dict(kind="bomb",    name="Grenades x3",   desc="+3 Grenade ammo",       cost=6,  col=C["lime"]),
]

# ─── CAMERA ───────────────────────────────────────────────────────────────────
class Camera:
    def __init__(self):
        self.pos=Vector2(0,0); self.target=Vector2(0,0)
        self.shake=0.0; self.smag=0.0; self._off=Vector2(0,0)
    def update(self,dt,tp,facing):
        look=facing*55 if facing.length()>0 else Vector2(0,0)
        self.target=tp+look; self.pos+=(self.target-self.pos)*min(9*dt,1.0)
        self._off.x=self._off.y=0
        if self.shake>0:
            self.shake-=dt; self.smag*=0.86
            self._off.x=random.uniform(-self.smag,self.smag)
            self._off.y=random.uniform(-self.smag,self.smag)
    def hit(self,m,d): self.smag=max(self.smag,m); self.shake=max(self.shake,d)
    def apply(self,wp): return Vector2(wp)-self.pos+Vector2(W/2,H/2)+self._off

# ─── PARTICLES ────────────────────────────────────────────────────────────────
class PS:
    def __init__(self):
        self.pts=[]; self.texts=[]; self.ghosts=[]; self.lasers=[]
    def emit(self,x,y,n,col,spd,life=.45,sz=3,dv=None,gravity=0):
        for _ in range(n):
            ang=(math.atan2(dv.y,dv.x)+random.uniform(-.7,.7)) if dv else random.uniform(0,math.pi*2)
            v=random.uniform(spd*.25,spd)
            self.pts.append([Vector2(x,y),Vector2(math.cos(ang)*v,math.sin(ang)*v),life*random.uniform(.6,1.4),life,col,sz,gravity])
    def emit_txt(self,x,y,text,col,big=False):
        self.texts.append([Vector2(x,y),Vector2(random.uniform(-35,35),random.uniform(-140,-80)),1.1,1.1,str(text),col,F_BIG if big else F_MED])
    def ghost(self,surf,pos): self.ghosts.append([surf.copy(),Vector2(pos),.18,.18])
    def add_laser(self,p1,p2,col,life=.08): self.lasers.append([Vector2(p1),Vector2(p2),col,life,life])
    def splat(self,x,y,col,n=4):
        for _ in range(n):
            ang=random.uniform(math.pi*.8,math.pi*2.2); spd2=random.uniform(20,80)
            self.pts.append([Vector2(x+random.uniform(-12,12),y+random.uniform(-8,8)),Vector2(math.cos(ang)*spd2,math.sin(ang)*spd2),random.uniform(.8,2.0),1.5,col,random.randint(4,10),60])
    def update(self,dt):
        for p in self.pts: p[0]+=p[1]*dt; p[1]*=0.87; p[1].y+=p[6]*dt; p[2]-=dt
        self.pts=[p for p in self.pts if p[2]>0]
        for t in self.texts: t[0]+=t[1]*dt; t[1].y+=320*dt; t[2]-=dt
        self.texts=[t for t in self.texts if t[2]>0]
        for g in self.ghosts: g[2]-=dt
        self.ghosts=[g for g in self.ghosts if g[2]>0]
        for l in self.lasers: l[3]-=dt
        self.lasers=[l for l in self.lasers if l[3]>0]
    def draw(self,surf,cam):
        for g in self.ghosts:
            p=cam.apply(g[1]); a=int(g[2]/g[3]*85); tmp=g[0].copy(); tmp.set_alpha(a); surf.blit(tmp,(p.x-18,p.y-20))
        for p in self.pts:
            pos=cam.apply(p[0])
            if not(-20<pos.x<W+20 and -20<pos.y<H+20): continue
            a=max(0,min(255,int(p[2]/p[3]*255))); sz=max(1,int(p[5]*(p[2]/p[3])))
            s2=pygame.Surface((sz*2,sz*2),pygame.SRCALPHA)
            pygame.draw.circle(s2,(*p[4],a),(sz,sz),sz); surf.blit(s2,(int(pos.x-sz),int(pos.y-sz)))
        for t in self.texts:
            p=cam.apply(t[0]); a=max(0,min(255,int(t[2]/t[3]*255)))
            txt=t[6].render(t[4],True,t[5]); txt.set_alpha(a)
            ol=t[6].render(t[4],True,C["black"]); ol.set_alpha(a)
            surf.blit(ol,(p.x-txt.get_width()//2+1,p.y-txt.get_height()//2+1))
            surf.blit(txt,(p.x-txt.get_width()//2,p.y-txt.get_height()//2))
        for l in self.lasers:
            p1=cam.apply(l[0]); p2=cam.apply(l[1]); a=max(0,min(255,int(l[3]/l[4]*220)))
            pygame.draw.line(surf,(*l[2],a),(int(p1.x),int(p1.y)),(int(p2.x),int(p2.y)),3)

# ─── COMBO ────────────────────────────────────────────────────────────────────
STREAK_LABELS=[(2,"DOUBLE KILL!",C["yellow"]),(3,"TRIPLE KILL!",C["orange"]),(5,"RAMPAGE!",C["red"]),(8,"UNSTOPPABLE!",C["purple"]),(12,"GODLIKE!!!",C["gold"])]
class ComboSystem:
    def __init__(self): self.count=0; self.timer=0.0; self.WINDOW=2.5; self.best=0; self.streak_t=0.0; self.streak_txt=""; self.streak_col=C["white"]
    def kill(self,ps,px,py):
        self.count+=1; self.timer=self.WINDOW; self.best=max(self.best,self.count)
        mult=1.0+min(self.count*.1,2.0)
        for thresh,label,col in reversed(STREAK_LABELS):
            if self.count==thresh:
                self.streak_txt=label; self.streak_col=col; self.streak_t=1.8
                ps.emit_txt(px,py-40,label,col,big=True); SFX.play("room_clear"); break
        return mult
    def update(self,dt):
        if self.timer>0:
            self.timer-=dt
            if self.timer<=0: self.count=0
        if self.streak_t>0: self.streak_t-=dt
    def draw(self,surf):
        if self.count<2: return
        a=min(255,int(self.timer/self.WINDOW*255)); col=C["gold"] if self.count>=5 else C["yellow"]
        txt=F_BIG.render(f"x{self.count} COMBO",True,col); txt.set_alpha(a)
        surf.blit(txt,(W//2-txt.get_width()//2,12))
        bw=int(200*max(0,self.timer/self.WINDOW))
        pygame.draw.rect(surf,(40,40,40),(W//2-100,46,200,5),border_radius=2)
        pygame.draw.rect(surf,col,(W//2-100,46,bw,5),border_radius=2)
        if bw>0: mt=F_SM.render(f"x{1+min(self.count*.1,2.0):.1f} SCORE",True,C["cyan"]); surf.blit(mt,(W//2-mt.get_width()//2,54))

# ─── DEATH ANIM ───────────────────────────────────────────────────────────────
class DeathAnim:
    def __init__(self,x,y,col,rad,etype):
        self.pos=Vector2(x,y); self.col=col; self.rad=rad; self.etype=etype
        self.t=0.0; self.dur=1.1 if etype=="boss" else 0.55; self.done=False
        n=14 if etype=="boss" else 7
        self.pieces=[]
        for _ in range(n):
            ang=random.uniform(0,math.pi*2); spd=random.uniform(60,180); sz=random.randint(3,rad//2+2)
            c2=tuple(max(0,min(255,col[i]+random.randint(-30,30))) for i in range(3))
            self.pieces.append([Vector2(x,y),Vector2(math.cos(ang)*spd,math.sin(ang)*spd),sz,c2])
    def update(self,dt):
        self.t+=dt; self.done=self.t>=self.dur
        for p in self.pieces: p[0]+=p[1]*dt; p[1]*=0.82; p[1].y+=120*dt
    def draw(self,surf,cam):
        if self.done: return
        ratio=1.0-self.t/self.dur; a=int(ratio*255)
        for p in self.pieces:
            pos=cam.apply(p[0]); sz=max(1,int(p[2]*ratio))
            s2=pygame.Surface((sz*2,sz*2),pygame.SRCALPHA)
            pygame.draw.rect(s2,(*p[3],a),(0,0,sz*2,sz*2)); surf.blit(s2,(int(pos.x-sz),int(pos.y-sz)))
        ring_r=int(self.rad*(1+self.t/self.dur*2)); ring_a=int(ratio*180)
        ring_s=pygame.Surface((ring_r*2+4,ring_r*2+4),pygame.SRCALPHA)
        pygame.draw.circle(ring_s,(*self.col,ring_a),(ring_r+2,ring_r+2),ring_r,2)
        rp=cam.apply(self.pos); surf.blit(ring_s,(int(rp.x-ring_r-2),int(rp.y-ring_r-2)))

class BloodDecal:
    MAX=60
    def __init__(self): self.decals=[]
    def splat(self,x,y,col,n=5):
        for _ in range(n):
            self.decals.append([Vector2(x+random.uniform(-20,20),y+random.uniform(-20,20)),col,random.randint(4,14),180,180])
        if len(self.decals)>self.MAX: self.decals=self.decals[-self.MAX:]
    def update(self,dt):
        for d in self.decals: d[3]=max(0,d[3]-dt*18)
        self.decals=[d for d in self.decals if d[3]>2]
    def draw(self,surf,cam):
        for d in self.decals:
            p=cam.apply(d[0])
            if not(-20<p.x<W+20 and -20<p.y<H+20): continue
            a=int(d[3]); sz=d[2]
            s2=pygame.Surface((sz*2,sz*2),pygame.SRCALPHA)
            pygame.draw.ellipse(s2,(*d[1],a),(0,sz//2,sz*2,sz)); surf.blit(s2,(int(p.x-sz),int(p.y-sz//2)))

class RoomClearFX:
    def __init__(self): self.active=False; self.t=0.0; self.dur=1.0
    def trigger(self,ps,px,py):
        self.active=True; self.t=self.dur
        ps.emit(px,py,40,C["gold"],280,.6,5); ps.emit(px,py,20,C["white"],180,.4,3)
        ps.emit_txt(px,py-60,"ROOM CLEAR!",C["gold"],big=True); SFX.play("room_clear")
    def update(self,dt):
        if self.active: self.t-=dt; self.active=self.t>0
    def draw(self,surf):
        if not self.active: return
        fl=pygame.Surface((W,H)); fl.fill((255,220,100)); fl.set_alpha(int(self.t/self.dur*55)); surf.blit(fl,(0,0))

# ─── ENTITY BASE ──────────────────────────────────────────────────────────────
class Ent:
    def __init__(self,x,y,rad,col):
        self.pos=Vector2(x,y); self.vel=Vector2(0,0); self.acc=Vector2(0,0)
        self.rad=rad; self.col=col; self.hp=self.mhp=100
        self.hit_t=0.0; self.facing=Vector2(1,0); self.t=random.uniform(0,100)
        self.statuses={}
    def forces(self,dt): self.vel+=self.acc*dt; self.acc*=0
    def drag(self,d): self.vel*=d
    def apply_status(self,kind):
        if kind=="freeze" and "freeze" in self.statuses: return
        self.statuses[kind]=StatusEffect(kind)
    def update_statuses(self,dt,ps):
        keep={}
        for k,s in self.statuses.items():
            if s.update(dt,self,ps): keep[k]=s
        self.statuses=keep
    def is_frozen(self): return "freeze" in self.statuses
    def is_stunned(self): return "stun" in self.statuses
    def move(self,dt,grid):
        sm=0.35 if self.is_frozen() else (0.5 if self.is_stunned() else 1.0)
        self.pos.x+=self.vel.x*dt*sm; self._wall(grid,'x')
        self.pos.y+=self.vel.y*dt*sm; self._wall(grid,'y')
        if self.hit_t>0: self.hit_t-=dt; self.t+=dt
    def _wall(self,grid,ax):
        col,row=int(self.pos.x//TILE),int(self.pos.y//TILE)
        for dr in(-1,0,1):
            for dc in(-1,0,1):
                r,c=row+dr,col+dc
                if 0<=r<len(grid) and 0<=c<len(grid[0]) and grid[r][c]=="wall":
                    wr=pygame.Rect(c*TILE,r*TILE,TILE,TILE)
                    er=pygame.Rect(self.pos.x-self.rad,self.pos.y-self.rad,self.rad*2,self.rad*2)
                    if er.colliderect(wr):
                        if ax=='x':
                            self.pos.x=wr.left-self.rad if self.vel.x>0 else wr.right+self.rad; self.vel.x=0
                        else:
                            self.pos.y=wr.top-self.rad if self.vel.y>0 else wr.bottom+self.rad; self.vel.y=0
    def hit(self,dmg,src,kb=500):
        self.hp-=dmg; self.hit_t=0.15
        d=self.pos-src
        if d.length()>0: self.vel+=d.normalize()*kb

# ─── PROJECTILE ───────────────────────────────────────────────────────────────
class Proj:
    def __init__(self,x,y,d,dmg,col,pspd=450,pierce=False,explosive=False,homing=False,gravity=0,flame=False,owner="player",wave=False,wave_freq=3.0):
        self.pos=Vector2(x,y); self.vel=d*pspd
        self.dmg=dmg; self.col=col; self.life=3.0; self.rad=6
        self.pierce=pierce; self.explosive=explosive; self.homing=homing
        self.gravity=gravity; self.flame=flame; self.owner=owner
        self.hit_enemies=set(); self.trail_t=0.0; self.status_on_hit=None
        self.wave=wave; self.wave_freq=wave_freq; self.wave_t=0.0; self._perp=Vector2(-d.y,d.x) if d.length()>0 else Vector2(0,1)
    def update(self,dt,targets=None):
        if self.homing and targets:
            n2=min(targets,key=lambda e:self.pos.distance_to(e.pos),default=None)
            if n2 and self.pos.distance_to(n2.pos)<350:
                d2=n2.pos-self.pos
                if d2.length()>0: self.vel+=(d2.normalize()*800-self.vel)*3*dt
        if self.wave:
            self.wave_t+=dt
            self.pos+=self._perp*math.sin(self.wave_t*self.wave_freq)*2
        self.vel.y+=self.gravity*dt; self.pos+=self.vel*dt; self.life-=dt; self.trail_t+=dt
    def draw(self,surf,cam):
        p=cam.apply(self.pos)
        if not(-20<p.x<W+20 and -20<p.y<H+20): return
        if self.flame:
            a=random.randint(100,200); sz=4+random.randint(0,3)
            s2=pygame.Surface((sz*2+4,sz*2+4),pygame.SRCALPHA)
            pygame.draw.circle(s2,(*self.col,a),(sz+2,sz+2),sz); surf.blit(s2,(int(p.x-sz-2),int(p.y-sz-2)))
        else:
            fk=random.randint(0,1)
            pygame.draw.circle(surf,self.col,(int(p.x),int(p.y)),self.rad+fk)
            pygame.draw.circle(surf,C["white"],(int(p.x),int(p.y)),self.rad//2)

# ─── DESTRUCTIBLE OBJECTS ─────────────────────────────────────────────────────
class Obj:
    TYPES={"crate":dict(hp=30,col=(120,80,40),col2=(80,50,20),drop=0.6,shape="rect",explodes=False),
           "barrel":dict(hp=20,col=(60,100,140),col2=(40,70,100),drop=0.4,shape="circle",explodes=True),
           "statue":dict(hp=50,col=(140,130,150),col2=(100,90,110),drop=0.3,shape="rect",explodes=False)}
    def __init__(self,x,y,kind="crate"):
        self.pos=Vector2(x,y); d=self.TYPES[kind]
        self.hp=self.mhp=d["hp"]; self.col=d["col"]; self.col2=d["col2"]
        self.drop=d["drop"]; self.shape=d["shape"]; self.kind=kind
        self.explodes=d["explodes"]; self.dead=False; self.hit_t=0.0
    def hit(self,dmg): self.hp-=dmg; self.hit_t=.12
    def update(self,dt):
        if self.hit_t>0: self.hit_t-=dt
    def draw(self,surf,cam):
        p=cam.apply(self.pos); c=C["white"] if self.hit_t>0 else self.col
        if self.shape=="circle":
            pygame.draw.circle(surf,c,(int(p.x),int(p.y)),14)
            pygame.draw.circle(surf,self.col2,(int(p.x),int(p.y)),14,2)
        else:
            pygame.draw.rect(surf,c,(int(p.x)-13,int(p.y)-13,26,26),border_radius=3)
            pygame.draw.rect(surf,self.col2,(int(p.x)-13,int(p.y)-13,26,26),2,border_radius=3)
            if self.kind=="crate":
                pygame.draw.line(surf,self.col2,(int(p.x)-13,int(p.y)-13),(int(p.x)+13,int(p.y)+13),1)
                pygame.draw.line(surf,self.col2,(int(p.x)+13,int(p.y)-13),(int(p.x)-13,int(p.y)+13),1)

class Loot:
    def __init__(self,x,y,kind,val=0,wkey=None):
        self.pos=Vector2(x,y); self.kind=kind; self.val=val; self.wkey=wkey
        self.t=random.uniform(0,100); self.collected=False
    def update(self,dt): self.t+=dt
    def draw(self,surf,cam):
        p=cam.apply(self.pos); bob=math.sin(self.t*3)*3
        if self.kind=="coin":
            pygame.draw.circle(surf,C["gold"],(int(p.x),int(p.y+bob)),8)
            pygame.draw.circle(surf,C["yellow"],(int(p.x),int(p.y+bob)),8,2)
        elif self.kind=="hp":
            pygame.draw.circle(surf,C["red"],(int(p.x),int(p.y+bob)),9)
            pygame.draw.line(surf,C["white"],(int(p.x),int(p.y+bob-5)),(int(p.x),int(p.y+bob+5)),3)
            pygame.draw.line(surf,C["white"],(int(p.x-5),int(p.y+bob)),(int(p.x+5),int(p.y+bob)),3)
        elif self.kind=="weapon" and self.wkey:
            wc=WEAPONS[self.wkey]["col"]; rc=RARITY_COL[WEAPONS[self.wkey]["rare"]]
            pygame.draw.circle(surf,(40,35,50),(int(p.x),int(p.y+bob)),13)
            pygame.draw.circle(surf,wc,(int(p.x),int(p.y+bob)),13,3)
            pygame.draw.circle(surf,rc,(int(p.x),int(p.y+bob)),16,1)
            lt=F_SM.render(self.wkey[:6],True,rc); surf.blit(lt,(int(p.x)-lt.get_width()//2,int(p.y+bob)+17))
        elif self.kind=="relic" and self.wkey:
            r=RELIC_BY_ID.get(self.wkey,{}); rc=RARITY_COL.get(r.get("rare","common"),(180,180,180))
            pygame.draw.circle(surf,r.get("col",C["gray"]),(int(p.x),int(p.y+bob)),12)
            pygame.draw.circle(surf,rc,(int(p.x),int(p.y+bob)),15,2)

class Turret:
    def __init__(self,x,y):
        self.pos=Vector2(x,y); self.hp=60; self.atk_cd=0.0; self.t=0.0
    def update(self,dt,enemies,projs,ps):
        self.t+=dt
        if self.atk_cd>0: self.atk_cd-=dt
        if not enemies: return
        nearest=min(enemies,key=lambda e:self.pos.distance_to(e.pos))
        if self.pos.distance_to(nearest.pos)<300 and self.atk_cd<=0:
            d=(nearest.pos-self.pos).normalize()
            projs.append(Proj(self.pos.x,self.pos.y,d,18,C["cyan"],pspd=480,owner="turret"))
            ps.emit(self.pos.x,self.pos.y,4,C["cyan"],80,.15,2,dv=d); self.atk_cd=0.5
    def draw(self,surf,cam):
        p=cam.apply(self.pos)
        pygame.draw.circle(surf,(60,80,60),(int(p.x),int(p.y)),12)
        pygame.draw.circle(surf,C["cyan"],(int(p.x),int(p.y)),12,2)
        ang=self.t*3; ep=Vector2(p.x+math.cos(ang)*14,p.y+math.sin(ang)*14)
        pygame.draw.line(surf,C["teal"],(int(p.x),int(p.y)),(int(ep.x),int(ep.y)),3)
        ratio=self.hp/60
        pygame.draw.rect(surf,C["black"],(int(p.x-12),int(p.y-18),24,4))
        pygame.draw.rect(surf,C["green"],(int(p.x-12),int(p.y-18),int(24*ratio),4))

# ─── PLAYER ───────────────────────────────────────────────────────────────────
ACCEL=5800; DRAG_ON=0.77; DRAG_OFF=0.68; DRAG_DASH=0.93; DASH_V=1600
class Player(Ent):
    def __init__(self,x,y):
        super().__init__(x,y,14,C["blue"])
        self.mhp=120; self.hp=120
        self.wkey="Pistol"; self.weapon=WEAPONS[self.wkey]
        self.ammo={k:MAX_AMMO[k] for k in MAX_AMMO}
        self.atk_t=0.0; self.dash_t=0.0; self.dash_cd=0.0; self.invuln=0.0
        self.xp=0; self.level=1; self.xp_next=50; self.crit=0.15
        self.aim_ang=0.0; self.swing_ang=0.0; self.swinging=False; self.coins=0
        self.spin_cd=0.0; self.shield_cd=0.0; self.shield_t=0.0
        self.turret_cd=0.0; self.recoil_t=0.0
        self._hit_this_swing=set()
        self._skill_q_held=False; self._skill_e_held=False; self._skill_r_held=False
        self.charge_t=0.0; self.MAX_CHARGE=0.6
        self.relics=[]; self.regen_t=0.0; self.revive_used=False
        # Class system defaults (overridden by apply_class)
        self.class_name       = "Warrior"
        self._class_speed     = 1.0
        self.dash_cd_base     = 0.85
        self.max_dashes       = 1
        self.dashes_left      = 1
        self._passive_dmg_reduce = 0.0
        self._backstab        = False
        self._free_skill_t    = 0.0
        self._free_skill_cd   = 999
        self._shadow_strike_ready = False
        self._shadow_strike_next  = False
        upg=META.get("upgrades",{})
        self.mhp+=upg.get("hp_up",0)*15; self.hp=self.mhp
        self.crit+=upg.get("crit_up",0)*0.05
        self._meta_dmg=1.0+upg.get("dmg_up",0)*0.08
        self._coin_magnet=upg.get("coin_mag",0)>=1

    def stats(self):
        dm=self._meta_dmg
        if "berserker" in self.relics and self.hp<self.mhp*.3: dm*=1.4
        if "glass_cannon" in self.relics: dm*=1.6
        sm=self._class_speed*(1.25 if "swift_boots" in self.relics else 1.0)
        return dict(dmg=int(self.weapon["dmg"]*(1+self.level*.15)*dm),
                    spd=max(120,self.weapon["spd"]*(1-self.level*.04)),
                    speed_mult=sm)

    def update(self,dt,grid,keys,mw,ps,cam,enemies,projs,turrets):
        for attr in("dash_cd","atk_t","invuln","spin_cd","shield_cd","shield_t","turret_cd","recoil_t"):
            setattr(self,attr,max(0.0,getattr(self,attr)-dt))
        if "iron_heart" in self.relics:
            self.regen_t+=dt
            if self.regen_t>=5.0: self.regen_t=0; self.hp=min(self.mhp,self.hp+1)
        s=self.stats(); sm=s.get("speed_mult",1.0)
        mx2=int(keys[pygame.K_d] or keys[pygame.K_RIGHT])-int(keys[pygame.K_a] or keys[pygame.K_LEFT])
        my2=int(keys[pygame.K_s] or keys[pygame.K_DOWN])-int(keys[pygame.K_w] or keys[pygame.K_UP])
        mv=Vector2(mx2,my2); has_in=mv.length()>0
        if self.dash_t>0:
            self.dash_t-=dt; self.invuln=self.dash_t+0.06; self.drag(DRAG_DASH)
            g=pygame.Surface((36,40),pygame.SRCALPHA); self._draw_body(g,Vector2(18,20),ghost=True)
            ps.ghost(g,self.pos); ps.emit(self.pos.x,self.pos.y+12,1,C["cyan"],30,.12,2)
        else:
            if has_in:
                mv=mv.normalize(); self.facing=mv; self.acc=mv*ACCEL*sm; self.forces(dt); self.drag(DRAG_ON)
            else: self.drag(DRAG_OFF)
            if keys[pygame.K_SPACE] and self.dash_cd<=0 and has_in:
                self.charge_t=min(self.charge_t+dt,self.MAX_CHARGE)
            elif not keys[pygame.K_SPACE] and self.charge_t>0 and self.dash_cd<=0:
                cr=self.charge_t/self.MAX_CHARGE
                self.vel=self.facing*DASH_V*(1+cr*.8); self.dash_t=0.15+cr*.12; self.dash_cd=max(0.5,.85-cr*.25)
                col_d=C["gold"] if cr>.7 else C["cyan"]
                cam.hit(3+int(cr*5),.08+cr*.1)
                ps.emit(self.pos.x,self.pos.y,int(8+cr*16),col_d,200+int(cr*150),.22,3)
                SFX.play("dash_full" if cr>.7 else "dash"); self.charge_t=0.0
            elif not keys[pygame.K_SPACE]: self.charge_t=0.0
        self.move(dt,grid)
        d2=mw-self.pos
        if d2.length()>0: self.aim_ang=math.degrees(math.atan2(-d2.y,d2.x))

        # Free skill passive (Mage)
        if self._free_skill_cd < 999:
            self._free_skill_t+=dt
            if self._free_skill_t>=self._free_skill_cd:
                self._free_skill_t=0
                ps.emit_txt(self.pos.x,self.pos.y-30,"FREE SKILL",C["purple"])

        # ── SKILLS (class-specific) ──
        q=bool(keys[pygame.K_q])
        free=self._free_skill_t>=self._free_skill_cd*0.99
        if q and not self._skill_q_held and self.spin_cd<=0:
            if self.class_name=="Warrior":
                # WHIRLWIND: big spin + lifesteal
                healed=0
                for e in enemies:
                    if self.pos.distance_to(e.pos)<110:
                        dmg=self.stats()["dmg"]*1.5; e.hit(int(dmg),self.pos,700)
                        ps.emit(e.pos.x,e.pos.y,14,C["orange"],240)
                        healed+=int(dmg*0.10)
                self.hp=min(self.mhp,self.hp+healed)
                if healed>0: ps.emit_txt(self.pos.x,self.pos.y-30,f"+{healed}",C["green"])
                ps.emit(self.pos.x,self.pos.y,30,C["orange"],200,.4,5)
                cam.hit(6,.15); self.spin_cd=4.0
            elif self.class_name=="Mage":
                # NOVA: magic explosion
                for e in enemies:
                    if self.pos.distance_to(e.pos)<200: e.hit(150,self.pos,800)
                ps.emit(self.pos.x,self.pos.y,50,C["purple"],350,.6,6)
                ps.emit(self.pos.x,self.pos.y,20,C["white"],200,.3,3)
                cam.hit(10,.3); self.spin_cd=5.0 if not free else 0.1
                if free: self._free_skill_t=0
            elif self.class_name=="Rogue":
                # SHADOWSTRIKE: next hit is 3x crit
                self._shadow_strike_next=True
                ps.emit(self.pos.x,self.pos.y,18,C["cyan"],160,.3,3)
                ps.emit_txt(self.pos.x,self.pos.y-25,"SHADOWSTRIKE!",C["cyan"])
                self.spin_cd=3.0
        self._skill_q_held=q

        en=bool(keys[pygame.K_e])
        if en and not self._skill_e_held and self.shield_cd<=0:
            if self.class_name=="Warrior":
                # IRON SKIN: damage reduction
                self.shield_t=4.0; self.shield_cd=12.0
                ps.emit(self.pos.x,self.pos.y,20,C["orange"],150,.4,4); SFX.play("shield")
            elif self.class_name=="Mage":
                # BLINK: teleport to cursor
                blink_pos=Vector2(mw)
                # Check not in wall
                bc2=int(blink_pos.x//TILE); br2=int(blink_pos.y//TILE)
                if 0<=br2<len(grid) and 0<=bc2<len(grid[0]) and grid[br2][bc2]=="floor":
                    ps.emit(self.pos.x,self.pos.y,20,C["purple"],180,.3,3)
                    self.pos=blink_pos
                    ps.emit(self.pos.x,self.pos.y,20,C["cyan"],180,.3,3)
                    self.invuln=0.5
                self.shield_cd=8.0 if not free else 0.1
                if free: self._free_skill_t=0
            elif self.class_name=="Rogue":
                # SMOKE BOMB: invuln + poison cloud
                self.shield_t=2.0; self.shield_cd=10.0
                ps.emit(self.pos.x,self.pos.y,30,C["lime"],130,.5,4)
                for e in enemies:
                    if self.pos.distance_to(e.pos)<80: e.apply_status("poison")
                SFX.play("shield")
        self._skill_e_held=en

        r=bool(keys[pygame.K_r])
        if r and not self._skill_r_held and self.turret_cd<=0:
            if self.class_name=="Warrior":
                # WAR CRY: dmg buff + stun nearby
                self._war_cry_t=5.0
                for e in enemies:
                    if self.pos.distance_to(e.pos)<120: e.apply_status("stun")
                ps.emit(self.pos.x,self.pos.y,25,C["gold"],200,.4,4)
                ps.emit_txt(self.pos.x,self.pos.y-35,"WAR CRY!",C["gold"],True)
                self.turret_cd=15.0
            elif self.class_name=="Mage":
                # METEOR: 5 meteors at cursor
                for i in range(5):
                    ang2=random.uniform(0,math.pi*2); dist2=random.uniform(0,80)
                    tx=mw.x+math.cos(ang2)*dist2; ty=mw.y+math.sin(ang2)*dist2
                    projs.append(Proj(self.pos.x,self.pos.y-200,
                        Vector2(tx-self.pos.x,ty-self.pos.y+200).normalize(),
                        80,C["red"],pspd=400,explosive=True,gravity=300))
                self.turret_cd=12.0 if not free else 0.1
                if free: self._free_skill_t=0
                ps.emit(self.pos.x,self.pos.y,15,C["red"],150,.3,4)
            elif self.class_name=="Rogue":
                # FAN OF KNIVES: 12 daggers
                for i in range(12):
                    ang3=math.radians(i*30)
                    dv3=Vector2(math.cos(ang3),math.sin(ang3))
                    projs.append(Proj(self.pos.x,self.pos.y,dv3,
                        self.stats()["dmg"],C["cyan"],pspd=550,pierce=True))
                cam.hit(5,.12); self.turret_cd=8.0
                ps.emit(self.pos.x,self.pos.y,20,C["cyan"],200,.3,3)
            else:
                turrets.append(Turret(self.pos.x+32,self.pos.y+32)); self.turret_cd=12.0
        self._skill_r_held=r

        # War cry dmg bonus (Warrior)
        if not hasattr(self,"_war_cry_t"): self._war_cry_t=0
        if self._war_cry_t>0: self._war_cry_t=max(0,self._war_cry_t-dt)

    def fire(self,projs,ps,cam):
        if self.atk_t>0: return None
        s=self.stats(); wep=self.weapon; cls=wep["cls"]
        if cls=="melee":
            self.atk_t=s["spd"]/1000.0; self.swinging=True; self.swing_ang=self.aim_ang+wep["arc"]/2
            self._hit_this_swing=set()
            av=Vector2(math.cos(math.radians(self.aim_ang)),-math.sin(math.radians(self.aim_ang)))
            self.vel+=av*320; cam.hit(3,.05); SFX.play(wep.get("sfx","melee")); return "melee"
        key=self.wkey
        if key in self.ammo and self.ammo[key]<=0:
            ps.emit_txt(self.pos.x,self.pos.y-20,"RELOAD",C["red"]); return None
        if key in self.ammo: self.ammo[key]-=1
        self.atk_t=s["spd"]/1000.0; self.recoil_t=0.08
        av=Vector2(math.cos(math.radians(self.aim_ang)),-math.sin(math.radians(self.aim_ang)))
        self.vel-=av*200; cam.hit(2,.04)
        mx3=self.pos.x+av.x*20; my3=self.pos.y+av.y*20
        ps.emit(mx3,my3,8,wep["col"],180,.12,3,dv=av)
        if cls=="flame":
            for _ in range(wep["pellets"]):
                ang=self.aim_ang+random.uniform(-wep["spread"],wep["spread"])
                dv=Vector2(math.cos(math.radians(ang)),-math.sin(math.radians(ang)))
                projs.append(Proj(mx3,my3,dv,wep["dmg"],wep["col"],pspd=wep["pspd"]+random.randint(-40,40),flame=True))
        elif cls=="laser":
            for _ in range(3):
                ang=self.aim_ang+random.uniform(-2,2)
                dv=Vector2(math.cos(math.radians(ang)),-math.sin(math.radians(ang)))
                projs.append(Proj(mx3,my3,dv,wep["dmg"],wep["col"],pspd=wep["pspd"]+random.randint(0,60),pierce=True))
        else:
            for _ in range(wep["pellets"]):
                ang=self.aim_ang+random.uniform(-wep["spread"]/2,wep["spread"]/2)
                dv=Vector2(math.cos(math.radians(ang)),-math.sin(math.radians(ang)))
                grav=250 if wep.get("explosive") else 0
                p3=Proj(mx3,my3,dv,s["dmg"]//max(1,wep["pellets"])+1,wep["col"],
                    pspd=wep["pspd"],pierce=wep["pierce"],explosive=wep["explosive"],homing=wep["homing"],gravity=grav)
                if "inferno" in self.relics: p3.status_on_hit="burn"
                elif "toxic_rounds" in self.relics: p3.status_on_hit="poison"
                projs.append(p3)
        ps.add_laser(Vector2(mx3,my3),Vector2(mx3+av.x*60,my3+av.y*60),wep["col"])
        SFX.play(wep.get("sfx","shoot")); return "ranged"

    def do_melee_hits(self,enemies,ps,cam,slow_cb=None):
        s=self.stats()
        war_cry_bonus = 1.5 if getattr(self,"_war_cry_t",0)>0 else 1.0
        for e in enemies:
            if self.pos.distance_to(e.pos)<self.weapon["rng"]+e.rad:
                at=math.degrees(math.atan2(-(e.pos.y-self.pos.y),e.pos.x-self.pos.x))
                diff=(at-self.aim_ang+180)%360-180
                if abs(diff)<=self.weapon["arc"]/2 and id(e) not in self._hit_this_swing:
                    ic=random.random()<self.crit
                    # Shadowstrike: 3x crit
                    shadow=getattr(self,"_shadow_strike_next",False)
                    mult=3 if shadow else (2 if ic else 1)
                    dmg=int(s["dmg"]*mult*war_cry_bonus)
                    # Backstab: check if hitting from behind
                    if self._backstab:
                        angle_diff=abs((math.degrees(math.atan2(e.vel.y,e.vel.x))-self.aim_ang+180)%360-180)
                        if angle_diff<60: dmg=int(dmg*1.8); ps.emit_txt(e.pos.x,e.pos.y-35,"BACKSTAB!",C["cyan"])
                    e.hit(dmg,self.pos,900 if ic else 450)
                    dv=(e.pos-self.pos); dv=dv.normalize() if dv.length()>0 else Vector2(1,0)
                    ps.emit(e.pos.x,e.pos.y,18,C["red"],300,dv=dv); ps.splat(e.pos.x,e.pos.y,e.col,n=3)
                    ps.emit_txt(e.pos.x,e.pos.y-20,str(dmg),C["gold"] if (ic or shadow) else C["white"],(ic or shadow))
                    if ic or shadow:
                        cam.hit(8,.14); SFX.play("crit")
                        if "time_warp" in self.relics and slow_cb: slow_cb(0.3)
                    if shadow: self._shadow_strike_next=False
                    if "frost_touch" in self.relics: e.apply_status("freeze"); SFX.play("freeze")
                    if "toxic_rounds" in self.relics: e.apply_status("poison")
                    if "bloodstone" in self.relics: self.hp=min(self.mhp,self.hp+int(dmg*.05))
                    SFX.play("melee"); self._hit_this_swing.add(id(e))

    def hit(self,dmg,src,kb=500):
        # Warrior passive: 15% damage reduction; Iron Skin: 60%
        reduce=self._passive_dmg_reduce
        if self.shield_t>0 and self.class_name=="Warrior": reduce=0.60
        dmg=max(1,int(dmg*(1-reduce)))
        self.hp-=dmg; self.hit_t=0.15
        d=self.pos-src
        if d.length()>0: self.vel+=d.normalize()*kb

    def _draw_body(self,surf,p,ghost=False):
        spd=min(self.vel.length()/260,1.0); bob=math.sin(self.t*14)*3*spd
        flip=hasattr(self,'aim_ang') and (90<self.aim_ang%360<270)
        tint=(255,100,100) if (self.hit_t>0 and not ghost) else None; a=160 if ghost else 255
        pygame.draw.ellipse(surf,(0,0,0,80),(int(p.x-12),int(p.y+10+bob),24,8))
        # Pick class-specific animated frame
        anim_list = CLASS_ANIMS.get(getattr(self,"class_name","Warrior"), ANIM_PLAYER)
        frame = get_anim_frame(anim_list,self.t,self.vel,self.swinging,self.atk_t)
        draw_sprite(surf,frame,p.x,p.y-2+bob,flip_x=flip,tint=tint,alpha=a)
        # Class aura ring
        if not ghost:
            aura_col = CLASS_AURA.get(getattr(self,"class_name","Warrior"), C["blue"])
            aura_a = 35 + int(math.sin(self.t*3)*15)
            ar = pygame.Surface((32,32),pygame.SRCALPHA)
            pygame.draw.circle(ar,(*aura_col,aura_a),(16,16),15,2)
            surf.blit(ar,(int(p.x-16),int(p.y-2+bob-8)))
        if not ghost and self.shield_t>0:
            sc2 = CLASS_AURA.get(getattr(self,"class_name","Warrior"),(60,200,180))
            pygame.draw.circle(surf,(*sc2,90),(int(p.x),int(p.y+bob)),24)
            pygame.draw.circle(surf,sc2,(int(p.x),int(p.y+bob)),24,2)
        # War cry visual (Warrior)
        if not ghost and getattr(self,"_war_cry_t",0)>0:
            wc_a = int(60+math.sin(self.t*8)*30)
            ws = pygame.Surface((40,40),pygame.SRCALPHA)
            pygame.draw.circle(ws,(*C["gold"],wc_a),(20,20),19,3)
            surf.blit(ws,(int(p.x-20),int(p.y-20+bob)))
        # Shadowstrike ready (Rogue)
        if not ghost and getattr(self,"_shadow_strike_next",False):
            ss_a = int(80+math.sin(self.t*10)*40)
            ss = pygame.Surface((36,36),pygame.SRCALPHA)
            pygame.draw.circle(ss,(*C["cyan"],ss_a),(18,18),17,2)
            surf.blit(ss,(int(p.x-18),int(p.y-18+bob)))

    def draw(self,surf,cam):
        p=cam.apply(self.pos); self._draw_body(surf,p)
        if self.charge_t>0 and self.dash_cd<=0:
            cr=self.charge_t/self.MAX_CHARGE; col_r=C["gold"] if cr>.7 else C["cyan"]
            r2=int(18+cr*10); a2=int(80+cr*120)
            rs=pygame.Surface((r2*2+4,r2*2+4),pygame.SRCALPHA)
            pygame.draw.circle(rs,(*col_r,a2),(r2+2,r2+2),r2,2); surf.blit(rs,(int(p.x-r2-2),int(p.y-r2-2)))
        wep=self.weapon
        if self.swinging:
            arc_spd=wep["arc"]/(self.stats()["spd"]/1000.0); self.swing_ang-=arc_spd*(1/FPS)
            if self.swing_ang<self.aim_ang-wep["arc"]/2:
                self.swinging=False; self._hit_this_swing=set()
            else:
                rad=math.radians(self.swing_ang); ep=p+Vector2(math.cos(rad),-math.sin(rad))*wep["rng"]
                pygame.draw.line(surf,C["white"],(int(p.x),int(p.y)),(int(ep.x),int(ep.y)),5)
                rr=pygame.Rect(p.x-wep["rng"],p.y-wep["rng"],wep["rng"]*2,wep["rng"]*2)
                pygame.draw.arc(surf,wep["col"],rr,math.radians(-self.aim_ang-wep["arc"]/2),math.radians(-self.swing_ang),16)
        elif wep["cls"]!="melee":
            rv=Vector2(math.cos(math.radians(self.aim_ang)),-math.sin(math.radians(self.aim_ang)))
            roff=rv*(-6*self.recoil_t/.08) if self.recoil_t>0 else Vector2(0,0)
            gp=p+rv*14+roff; tip=gp+rv*10
            pygame.draw.line(surf,wep["col"],(int(p.x),int(p.y)),(int(tip.x),int(tip.y)),4)
            pygame.draw.circle(surf,wep["col"],(int(tip.x),int(tip.y)),3)

# ─── ENEMIES (expanded) ───────────────────────────────────────────────────────
class Enemy(Ent):
    DEFS={
        "goblin": dict(col=C["green"],  rad=10,spd=1300,mhp=25, dmg=5, xpv=15, pat="erratic"),
        "orc":    dict(col=C["orange"], rad=16,spd=850, mhp=70, dmg=12,xpv=30, pat="charge"),
        "mage":   dict(col=C["purple"], rad=12,spd=700, mhp=32, dmg=8,xpv=40, pat="teleport"),
        "archer": dict(col=C["lime"],   rad=11,spd=650, mhp=28, dmg=9,xpv=35, pat="kite"),
        "bomber": dict(col=C["red"],    rad=12,spd=1000,mhp=35, dmg=20,xpv=45, pat="suicide"),
        "elite":  dict(col=(255,100,0), rad=14,spd=1100,mhp=110,dmg=11,xpv=90, pat="erratic"),
        # ── NEW ENEMIES ──
        "shielder":dict(col=C["silver"],rad=15,spd=750, mhp=90, dmg=12,xpv=60, pat="shield"),
        "warlock": dict(col=C["violet"],rad=12,spd=600, mhp=48, dmg=10,xpv=70, pat="warlock"),
        "knight":  dict(col=(80,80,160),rad=14,spd=800, mhp=80, dmg=14,xpv=65, pat="charge"),
        # ── BOSSES ──
        "boss":       dict(col=C["red"],   rad=30,spd=800, mhp=380, dmg=18,xpv=300, pat="boss"),
        "ice_queen":  dict(col=C["cyan"],  rad=28,spd=650, mhp=340, dmg=14,xpv=350, pat="ice_boss"),
        "death_lord": dict(col=C["crimson"],rad=32,spd=900, mhp=520,dmg=20,xpv=500, pat="death_boss"),
        "chaos_titan":dict(col=C["ember"], rad=36,spd=1000,mhp=750,dmg=24,xpv=800, pat="titan_boss"),
    }
    SPRITES={}
    def __init__(self,x,y,etype,floor):
        d=self.DEFS[etype]; super().__init__(x,y,d["rad"],d["col"])
        self.etype=etype; self.pat=d["pat"]
        sc=1+(floor-1)*.12
        self.spd=d["spd"]; self.mhp=int(d["mhp"]*sc); self.hp=self.mhp
        self.dmg=int(d["dmg"]*sc); self.xpv=int(d["xpv"]*sc)
        self.atk_cd=0.0; self.timer=0.0; self.cdir=Vector2(0,0)
        self.phase=1; self.spiral_ang=0.0; self.state="wander"
        self.shield_up=False; self.shield_hp=40 if etype=="shielder" else 0
        self.enrage_t=0.0

    def ai(self,dt,player,grid,projs,ps):
        dist=self.pos.distance_to(player.pos); self.timer-=dt
        if self.atk_cd>0: self.atk_cd-=dt
        pat=self.pat

        # Boss phase transitions
        if pat in("boss","ice_boss","death_boss","titan_boss"):
            if self.hp<self.mhp*.35 and self.phase<3:
                self.phase=3; ps.emit_txt(self.pos.x,self.pos.y-50,"ENRAGE!",C["red"],True); SFX.play("boss_spawn")
                self.enrage_t=0.5
            elif self.hp<self.mhp*.65 and self.phase<2:
                self.phase=2; ps.emit_txt(self.pos.x,self.pos.y-50,"PHASE 2",C["orange"],True)

        if self.enrage_t>0:
            self.enrage_t-=dt
            ps.emit(self.pos.x,self.pos.y,3,self.col,100,.3,4)

        # ── PATTERNS ──
        if pat=="teleport":
            if dist<130 and self.timer<=0:
                ps.emit(self.pos.x,self.pos.y,22,C["purple"],120)
                ang=random.uniform(0,math.pi*2); self.pos+=Vector2(math.cos(ang),math.sin(ang))*210
                ps.emit(self.pos.x,self.pos.y,22,C["cyan"],100); self.timer=2.5
            elif dist<380 and self.atk_cd<=0:
                d3=(player.pos-self.pos).normalize()
                for off in(-15,0,15):
                    ang2=math.atan2(d3.y,d3.x)+math.radians(off)
                    dv=Vector2(math.cos(ang2),math.sin(ang2))
                    projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg,C["purple"],pspd=320,owner="enemy"))
                self.atk_cd=2.0

        elif pat=="charge":
            if self.timer<=0:
                if dist<230:
                    self.cdir=(player.pos-self.pos).normalize(); self.timer=1.0; self.state="chase"
                    ps.emit_txt(self.pos.x,self.pos.y-24,"!!",C["red"])
                elif dist<450: self.acc=(player.pos-self.pos).normalize()*self.spd; self.state="chase"
            else:
                self.acc=self.cdir*self.spd*3.8

        elif pat=="kite":
            if dist>200:   self.acc=(player.pos-self.pos).normalize()*self.spd;  self.state="chase"
            elif dist<150: self.acc=-(player.pos-self.pos).normalize()*self.spd; self.state="wander"
            if self.atk_cd<=0 and dist<360:
                d3=(player.pos-self.pos).normalize()
                projs.append(Proj(self.pos.x,self.pos.y,d3,self.dmg,C["lime"],pspd=400,owner="enemy"))
                self.atk_cd=1.2

        elif pat=="suicide":
            if dist<320: self.acc=(player.pos-self.pos).normalize()*self.spd*1.3; self.state="chase"
            if dist<self.rad+15:
                projs.append(Proj(self.pos.x,self.pos.y,Vector2(1,0),self.dmg*3,C["orange"],pspd=1,explosive=True,owner="enemy"))
                self.hp=0

        elif pat=="shield":
            # Shielder: immune from front, only take dmg from behind
            if dist<300: self.acc=(player.pos-self.pos).normalize()*self.spd; self.state="chase"
            self.shield_up=True

        elif pat=="warlock":
            # Warlock: summons wave bullets + teleports
            if dist<100 and self.timer<=0:
                ps.emit(self.pos.x,self.pos.y,18,C["violet"],100)
                ang=random.uniform(0,math.pi*2); self.pos+=Vector2(math.cos(ang),math.sin(ang))*180
                ps.emit(self.pos.x,self.pos.y,18,C["pink"],100); self.timer=1.8
            elif dist<350 and self.atk_cd<=0:
                d3=(player.pos-self.pos).normalize()
                for i in range(3):
                    ang2=math.atan2(d3.y,d3.x)+math.radians((i-1)*20)
                    dv=Vector2(math.cos(ang2),math.sin(ang2))
                    projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg,C["violet"],pspd=280,owner="enemy",wave=True,wave_freq=3.5))
                self.atk_cd=1.6

        elif pat=="boss":
            sm=1+.5*(self.phase-1)
            if dist<500: self.acc=(player.pos-self.pos).normalize()*self.spd*sm; self.state="chase"
            if self.atk_cd<=0:
                if self.phase==1:
                    for i in range(8):
                        ang=math.radians(i*45); dv=Vector2(math.cos(ang),math.sin(ang))
                        projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["red"],pspd=300,owner="enemy"))
                    self.atk_cd=2.5
                elif self.phase==2:
                    for i in range(12):
                        ang=self.spiral_ang+math.radians(i*30); dv=Vector2(math.cos(ang),math.sin(ang))
                        projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["orange"],pspd=280,owner="enemy"))
                    self.spiral_ang+=math.radians(18); self.atk_cd=1.6
                else:
                    for i in range(20):
                        ang=math.radians(i*18); dv=Vector2(math.cos(ang),math.sin(ang))
                        projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["pink"],pspd=260,homing=True,owner="enemy"))
                    self.atk_cd=1.8

        elif pat=="ice_boss":
            sm=1+.6*(self.phase-1)
            if dist<500: self.acc=(player.pos-self.pos).normalize()*self.spd*sm; self.state="chase"
            if self.atk_cd<=0:
                if self.phase==1:
                    # Ice ring
                    for i in range(10):
                        ang=math.radians(i*36); dv=Vector2(math.cos(ang),math.sin(ang))
                        p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["cyan"],pspd=220,owner="enemy")
                        p3.status_on_hit="freeze"; projs.append(p3)
                    self.atk_cd=2.8
                elif self.phase==2:
                    # Double spiral
                    for off in(0,math.pi):
                        for i in range(8):
                            ang=self.spiral_ang+math.radians(i*45)+off; dv=Vector2(math.cos(ang),math.sin(ang))
                            p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,(100,220,255),pspd=240,owner="enemy")
                            p3.status_on_hit="freeze"; projs.append(p3)
                    self.spiral_ang+=math.radians(20); self.atk_cd=1.4
                else:
                    # Homing blizzard
                    for i in range(16):
                        ang=math.radians(i*22.5); dv=Vector2(math.cos(ang),math.sin(ang))
                        p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["cyan"],pspd=200,homing=True,owner="enemy")
                        p3.status_on_hit="freeze"; projs.append(p3)
                    self.atk_cd=1.6

        elif pat=="death_boss":
            sm=1+.7*(self.phase-1)
            if dist<500: self.acc=(player.pos-self.pos).normalize()*self.spd*sm; self.state="chase"
            if self.atk_cd<=0:
                if self.phase==1:
                    # Poison spread
                    d3=(player.pos-self.pos).normalize()
                    for off in range(-4,5):
                        ang=math.atan2(d3.y,d3.x)+math.radians(off*12)
                        dv=Vector2(math.cos(ang),math.sin(ang))
                        p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["lime"],pspd=260,owner="enemy")
                        p3.status_on_hit="poison"; projs.append(p3)
                    self.atk_cd=2.2
                elif self.phase==2:
                    # Triple spiral with poison
                    for off in(0,math.pi*2/3,math.pi*4/3):
                        for i in range(6):
                            ang=self.spiral_ang+math.radians(i*60)+off
                            dv=Vector2(math.cos(ang),math.sin(ang))
                            p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,(80,200,60),pspd=250,owner="enemy")
                            p3.status_on_hit="poison"; projs.append(p3)
                    self.spiral_ang+=math.radians(25); self.atk_cd=1.2
                else:
                    # Exploding wave + homing poison
                    for i in range(12):
                        ang=math.radians(i*30); dv=Vector2(math.cos(ang),math.sin(ang))
                        p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["lime"],pspd=230,homing=True,owner="enemy")
                        p3.status_on_hit="poison"; projs.append(p3)
                    # Also drop explosive orbs
                    for i in range(4):
                        ang=math.radians(i*90); dv=Vector2(math.cos(ang),math.sin(ang))
                        projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg,C["orange"],pspd=150,explosive=True,owner="enemy"))
                    self.atk_cd=1.5

        elif pat=="titan_boss":
            sm=1+.8*(self.phase-1)
            if dist<600: self.acc=(player.pos-self.pos).normalize()*self.spd*sm; self.state="chase"
            if self.atk_cd<=0:
                if self.phase==1:
                    # Massive radial
                    for i in range(16):
                        ang=math.radians(i*22.5); dv=Vector2(math.cos(ang),math.sin(ang))
                        projs.append(Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["ember"],pspd=320,owner="enemy"))
                    self.atk_cd=2.0
                elif self.phase==2:
                    # All patterns combined
                    for i in range(24):
                        ang=self.spiral_ang+math.radians(i*15); dv=Vector2(math.cos(ang),math.sin(ang))
                        p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["ember"],pspd=290,owner="enemy")
                        p3.status_on_hit="burn"; projs.append(p3)
                    self.spiral_ang+=math.radians(30); self.atk_cd=1.0
                else:
                    # CHAOS: homing + wave + explosive
                    for i in range(20):
                        ang=math.radians(i*18); dv=Vector2(math.cos(ang),math.sin(ang))
                        ptype=random.choice(["homing","wave","explode"])
                        if ptype=="homing":
                            p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["pink"],pspd=250,homing=True,owner="enemy")
                        elif ptype=="wave":
                            p3=Proj(self.pos.x,self.pos.y,dv,self.dmg//2,C["ember"],pspd=280,owner="enemy",wave=True)
                        else:
                            p3=Proj(self.pos.x,self.pos.y,dv,self.dmg,C["orange"],pspd=200,explosive=True,owner="enemy")
                        projs.append(p3)
                    self.atk_cd=0.8

        else:  # erratic
            if dist<350:
                self.state="chase"
                if self.timer<=0:
                    direct=(player.pos-self.pos).normalize()
                    side=Vector2(-direct.y,direct.x)*random.choice((-1,1))
                    self.cdir=(direct+side*.7).normalize(); self.timer=random.uniform(.2,.5)
                self.acc=self.cdir*self.spd
            else:
                self.state="wander"
                if self.timer<=0:
                    self.cdir=Vector2(random.uniform(-1,1),random.uniform(-1,1)).normalize(); self.timer=random.uniform(1,3)
                self.acc=self.cdir*self.spd*.4

        # Melee contact
        is_boss_type=pat.endswith("boss")
        if dist<self.rad+15 and self.etype not in("mage","warlock"):
            if (hasattr(player,'hit_t') and player.hit_t<=0 and
                getattr(player,'invuln',0)<=0 and getattr(player,'shield_t',0)<=0):
                player.hit(self.dmg,self.pos,700)

        self.forces(dt); self.drag(0.85 if pat=="charge" and self.timer>0 else 0.83)
        self.move(dt,grid)

    def is_boss(self): return self.pat.endswith("boss")

    def draw(self,surf,cam):
        p=cam.apply(self.pos)
        if not(-35<p.x<W+35 and -35<p.y<H+35): return
        bob=math.sin(self.t*12)*2 if self.vel.length()>15 else 0
        tint2=(255,100,100) if self.hit_t>0 else None; flip2=self.vel.x<-.5
        pygame.draw.ellipse(surf,(0,0,0),(int(p.x-self.rad),int(p.y+self.rad-4),self.rad*2,8))
        spr=self.SPRITES.get(self.etype)
        # Use animated frames if available
        ANIM_MAP={"goblin":ANIM_GOBLIN,"orc":ANIM_ORC,"mage":ANIM_MAGE}
        anim=ANIM_MAP.get(self.etype)
        if anim:
            frame=get_anim_frame(anim,self.t,self.vel,False,0)
            draw_sprite(surf,frame,p.x,p.y+bob,flip_x=flip2,tint=tint2)
        elif spr:
            draw_sprite(surf,spr,p.x,p.y+bob,flip_x=flip2,tint=tint2)
        else:
            col=C["white"] if self.hit_t>0 else self.col
            if self.etype=="orc":
                rr=pygame.Rect(int(p.x-self.rad),int(p.y-self.rad+bob),self.rad*2,self.rad*2)
                pygame.draw.rect(surf,col,rr,border_radius=4); pygame.draw.rect(surf,C["black"],rr,2,border_radius=4)
                for sx in(-1,1): pygame.draw.line(surf,C["white"],(int(p.x+sx*4),int(p.y+self.rad-2+bob)),(int(p.x+sx*7),int(p.y+self.rad+5+bob)),2)
            elif self.is_boss():
                pc={1:self.col,2:C["orange"],3:C["pink"]}.get(self.phase,self.col)
                if self.etype=="ice_queen": pc={1:C["cyan"],2:(150,230,255),3:C["white"]}.get(self.phase,C["cyan"])
                elif self.etype=="death_lord": pc={1:C["crimson"],2:C["lime"],3:C["purple"]}.get(self.phase,C["crimson"])
                elif self.etype=="chaos_titan": pc={1:C["ember"],2:C["gold"],3:(255,50,50)}.get(self.phase,C["ember"])
                pygame.draw.circle(surf,col,(int(p.x),int(p.y+bob)),self.rad)
                pygame.draw.circle(surf,pc,(int(p.x),int(p.y+bob)),self.rad,4+self.phase)
                for sx in(-1,1): pygame.draw.line(surf,pc,(int(p.x+sx*12),int(p.y-self.rad+bob)),(int(p.x+sx*22),int(p.y-self.rad-16+bob)),3)
                # Boss name tag
                nm={"boss":"WARLORD","ice_queen":"ICE QUEEN","death_lord":"DEATH LORD","chaos_titan":"CHAOS TITAN"}.get(self.etype,"BOSS")
                nt=F_SM.render(f"[{nm} P{self.phase}]",True,pc); surf.blit(nt,(int(p.x)-nt.get_width()//2,int(p.y)-self.rad-26))
            elif self.etype=="shielder":
                pygame.draw.circle(surf,col,(int(p.x),int(p.y+bob)),self.rad)
                # Shield arc in front
                if self.shield_up:
                    d3=(pygame.mouse.get_pos()[0]-p.x, pygame.mouse.get_pos()[1]-p.y)
                    pygame.draw.arc(surf,C["silver"],pygame.Rect(int(p.x-self.rad-4),int(p.y-self.rad-4+bob),
                                    (self.rad+4)*2,(self.rad+4)*2),-.8,.8,4)
            elif self.etype=="warlock":
                pts=[(p.x,p.y-self.rad+bob),(p.x-self.rad,p.y+bob),(p.x,p.y+self.rad+bob),(p.x+self.rad,p.y+bob)]
                pygame.draw.polygon(surf,col,pts); pygame.draw.polygon(surf,C["violet"],pts,2)
            elif self.etype=="knight":
                pygame.draw.circle(surf,col,(int(p.x),int(p.y+bob)),self.rad)
                pygame.draw.circle(surf,(80,80,160),(int(p.x),int(p.y+bob)),self.rad,3)
            else:
                pygame.draw.circle(surf,col,(int(p.x),int(p.y+bob)),self.rad)
                pygame.draw.circle(surf,C["black"],(int(p.x),int(p.y+bob)),self.rad,2)

        if self.hp<self.mhp:
            ratio=max(0,self.hp/self.mhp); bw=self.rad*2+6
            pygame.draw.rect(surf,C["black"],(int(p.x-bw//2),int(p.y-self.rad-14),bw,5))
            bc=C["red"] if ratio<.3 else (C["orange"] if ratio<.6 else C["green"])
            pygame.draw.rect(surf,bc,(int(p.x-bw//2),int(p.y-self.rad-14),int(bw*ratio),5))
        # Alert ring
        if self.state=="chase" or (self.timer>0 and self.pat=="charge"):
            ar=self.rad+5
            a_ring=pygame.Surface((ar*2+2,ar*2+2),pygame.SRCALPHA)
            ring_col=(255,80,80) if self.is_boss() else (255,180,60)
            pygame.draw.circle(a_ring,(*ring_col,55),(ar+1,ar+1),ar,2); surf.blit(a_ring,(int(p.x-ar-1),int(p.y-ar-1)))
        # Status icons
        for idx,(k,s) in enumerate(self.statuses.items()): s.draw_icon(surf,int(p.x),int(p.y-self.rad-8),idx)

Enemy.SPRITES={"goblin":SPRITE_GOBLIN,"orc":SPRITE_ORC,"mage":SPRITE_MAGE,"archer":SPRITE_ARCHER,"elite":SPRITE_ELITE}

# ─── MAP GENERATION ───────────────────────────────────────────────────────────
ROOM_TYPES=["normal","normal","normal","normal","treasure","trap","shop"]

# Floor theme changes based on depth
def floor_theme(floor):
    if floor<=3:   return 0  # stone
    elif floor<=6: return 1  # cave
    else:          return 2  # lava

def gen_map(floor):
    cols,rows=50,40; grid=[["wall"]*cols for _ in range(rows)]
    rooms=[]; torches=[]; rtypes=[]
    theme=floor_theme(floor)
    n_rooms=min(28,20+floor//2)
    for _ in range(n_rooms):
        w,h=random.randint(4,10),random.randint(4,9)
        x,y=random.randint(2,cols-w-2),random.randint(2,rows-h-2)
        if any(x<rx+rw+2 and x+w+2>rx and y<ry+rh+2 and y+h+2>ry for rx,ry,rw,rh in rooms): continue
        rooms.append((x,y,w,h)); rtypes.append(random.choice(ROOM_TYPES) if len(rooms)>1 else "normal")
        for dy in range(h):
            for dx in range(w): grid[y+dy][x+dx]="floor"
        # Torch placement based on room type
        rt_now = rtypes[-1]
        if rt_now=="treasure":
            # 4 corner torches
            for tx,ty in [(x+1,y+1),(x+w-2,y+1),(x+1,y+h-2),(x+w-2,y+h-2)]:
                torches.append(Vector2(tx*TILE+TILE//2,ty*TILE+TILE//2))
        elif rt_now=="shop":
            torches.append(Vector2((x+w//2)*TILE,(y+h//2)*TILE))
            torches.append(Vector2((x+1)*TILE,(y+h//2)*TILE))
            torches.append(Vector2((x+w-2)*TILE,(y+h//2)*TILE))
        elif random.random()<.55:
            torches.append(Vector2((x+w//2)*TILE,(y+h//2)*TILE))
    for i in range(1,len(rooms)):
        r1,r2=rooms[i-1],rooms[i]
        x1,y1=r1[0]+r1[2]//2,r1[1]+r1[3]//2; x2,y2=r2[0]+r2[2]//2,r2[1]+r2[3]//2
        if random.random()<.5:
            for cx2 in range(min(x1,x2),max(x1,x2)+1): grid[y1][cx2]="floor"
            for cy2 in range(min(y1,y2),max(y1,y2)+1): grid[cy2][x2]="floor"
        else:
            for cy2 in range(min(y1,y2),max(y1,y2)+1): grid[cy2][x1]="floor"
            for cx2 in range(min(x1,x2),max(x1,x2)+1): grid[y2][cx2]="floor"
    start=Vector2((rooms[0][0]+rooms[0][2]//2)*TILE,(rooms[0][1]+rooms[0][3]//2)*TILE)
    end=Vector2((rooms[-1][0]+rooms[-1][2]//2)*TILE,(rooms[-1][1]+rooms[-1][3]//2)*TILE)
    return grid,rooms,rtypes,torches,start,end,theme

def build_floor(grid,theme=0):
    rng2=random.Random(sum(len(r) for r in grid)+theme*999)
    cols=len(grid[0]); rows=len(grid)
    s=pygame.Surface((cols*TILE,rows*TILE)); s.fill(C["bg"])
    base_idx=theme*4

    # Draw floor tiles
    for r in range(rows):
        for c in range(cols):
            if grid[r][c]=="floor":
                idx=base_idx+rng2.randint(0,3)
                s.blit(FLOOR_V[min(idx,len(FLOOR_V)-1)],(c*TILE,r*TILE))

    # Floor decorations per theme
    floor_cells=[(r,c) for r in range(rows) for c in range(cols) if grid[r][c]=="floor"]

    # Moss/cracks overlay (theme 0: stone)
    if theme==0:
        for r,c in floor_cells:
            if rng2.random()<0.08:
                x=c*TILE; y=r*TILE
                # Moss patch
                for _ in range(rng2.randint(3,8)):
                    mx2=x+rng2.randint(2,TILE-4); my2=y+rng2.randint(2,TILE-4)
                    mc=tuple(max(0,min(255,(30+rng2.randint(-8,8),60+rng2.randint(-8,8),30+rng2.randint(-8,8))[i])) for i in range(3))
                    pygame.draw.circle(s,(30+rng2.randint(0,20),55+rng2.randint(0,20),25+rng2.randint(0,15)),(mx2,my2),rng2.randint(2,5))

    # Crystal formations (theme 1: cave)
    elif theme==1:
        for r,c in floor_cells:
            if rng2.random()<0.06:
                x=c*TILE+rng2.randint(4,TILE-4); y=r*TILE+rng2.randint(4,TILE-4)
                # Small crystal cluster
                for i in range(rng2.randint(2,4)):
                    h2=rng2.randint(4,10); w2=rng2.randint(2,4)
                    cx3=x+rng2.randint(-6,6); cy3=y+rng2.randint(-4,4)
                    alpha=rng2.randint(100,200)
                    pts=[(cx3-w2,cy3+h2),(cx3+w2,cy3+h2),(cx3,cy3)]
                    pygame.draw.polygon(s,(60+rng2.randint(0,60),180+rng2.randint(0,60),200+rng2.randint(0,55)),pts)

    # Lava cracks + glow pools (theme 2)
    elif theme==2:
        for r,c in floor_cells:
            if rng2.random()<0.05:
                x=c*TILE+rng2.randint(4,TILE-4); y=r*TILE+rng2.randint(4,TILE-4)
                # Lava pool
                ps2=pygame.Surface((22,14),pygame.SRCALPHA)
                pygame.draw.ellipse(ps2,(200+rng2.randint(0,55),60+rng2.randint(0,40),10,180),(0,0,22,14))
                s.blit(ps2,(x-11,y-7))

    # Room border highlights (slightly lighter floor near walls)
    for r in range(1,rows-1):
        for c in range(1,cols-1):
            if grid[r][c]=="floor":
                # Check if adjacent to wall
                adj_wall=any(grid[r+dr][c+dc]=="wall" for dr,dc in((-1,0),(1,0),(0,-1),(0,1)) if 0<=r+dr<rows and 0<=c+dc<cols)
                if adj_wall:
                    edge_s=pygame.Surface((TILE,TILE),pygame.SRCALPHA)
                    edge_col=[(50,40,60,40),(30,55,45,40),(60,25,15,40)][theme]
                    edge_s.fill(edge_col)
                    s.blit(edge_s,(c*TILE,r*TILE))

    return s

def spawn_objs(rooms,rtypes,floor):
    objs=[]; loots=[]
    for i,(room,rt) in enumerate(zip(rooms,rtypes)):
        rx,ry,rw,rh=room; cx=(rx+rw//2)*TILE; cy=(ry+rh//2)*TILE
        if rt=="treasure":
            rare_keys=[k for k,v in WEAPONS.items() if v["rare"] in("rare","epic","legendary")]
            loots.append(Loot(cx,cy,"weapon",wkey=random.choice(rare_keys)))
        elif rt=="trap":
            for _ in range(3):
                ox=(rx+random.randint(1,rw-1))*TILE; oy=(ry+random.randint(1,rh-1))*TILE
                objs.append(Obj(ox,oy,"barrel"))
        elif i>0:
            for _ in range(random.randint(1,3)):
                ox=(rx+random.randint(1,rw-1))*TILE; oy=(ry+random.randint(1,rh-1))*TILE
                objs.append(Obj(ox,oy,random.choice(["crate","barrel","crate"])))
    return objs,loots

def spawn_enemies(rooms,rtypes,floor):
    enemies=[]; theme=floor_theme(floor)
    # Boss schedule
    boss_schedule={1:"boss",2:"boss",5:"ice_queen",8:"death_lord",10:"chaos_titan"}
    is_boss=floor%5==0 or floor in boss_schedule
    # Enemy pool expands by floor
    if floor<=2:   pool=["goblin","orc"]; wts=[70,30]
    elif floor<=4: pool=["goblin","orc","mage","archer"]; wts=[50,25,15,10]
    elif floor<=6: pool=["goblin","orc","mage","archer","bomber","elite"]; wts=[40,22,14,10,8,6]
    else:          pool=["goblin","orc","mage","archer","bomber","elite","shielder","warlock","knight"]; wts=[30,18,13,10,8,6,5,5,5]
    for i,(room,rt) in enumerate(zip(rooms,rtypes)):
        if i==0 or rt=="treasure": continue
        rx,ry,rw,rh=room
        if is_boss and i==len(rooms)-1:
            bx=(rx+rw//2)*TILE; by=(ry+rh//2)*TILE
            btype=boss_schedule.get(floor,"boss")
            enemies.append(Enemy(bx,by,btype,floor)); SFX.play("boss_spawn"); continue
        n=random.randint(1,3+floor//2)
        for _ in range(n):
            ex=(rx+random.randint(0,rw-1))*TILE+TILE//2; ey=(ry+random.randint(0,rh-1))*TILE+TILE//2
            et=random.choices(pool,weights=wts)[0]; enemies.append(Enemy(ex,ey,et,floor))
    return enemies

# ─── MINIMAP ──────────────────────────────────────────────────────────────────
class Minimap:
    SIZE=160; PAD=16
    def __init__(self): self.revealed=set(); self.room_revealed=set(); self._surf=None; self._dirty=True; self._grid=None; self._rooms=None; self.sc=1.0
    def new_floor(self,grid,rooms):
        self.revealed.clear(); self.room_revealed.clear(); self._grid=grid; self._rooms=rooms; self._dirty=True
        self.sc=self.SIZE/max(len(grid[0]),len(grid))
    def update(self,player_pos,rooms):
        px=int(player_pos.x//TILE); py=int(player_pos.y//TILE)
        for i,(rx,ry,rw,rh) in enumerate(rooms):
            if rx<=px<rx+rw and ry<=py<ry+rh:
                if i not in self.room_revealed:
                    self.room_revealed.add(i)
                    for dr in range(-1,rh+1):
                        for dc in range(-1,rw+1):
                            r2=ry+dr; c2=rx+dc
                            if 0<=r2<len(self._grid) and 0<=c2<len(self._grid[0]) and self._grid[r2][c2]=="floor":
                                self.revealed.add((c2,r2))
                    self._dirty=True
    def _rebuild(self):
        grid=self._grid
        if not grid: return
        s=pygame.Surface((self.SIZE,self.SIZE),pygame.SRCALPHA)
        pygame.draw.rect(s,(0,0,0,185),(0,0,self.SIZE,self.SIZE),border_radius=10)
        pygame.draw.rect(s,(60,50,80,220),(0,0,self.SIZE,self.SIZE),1,border_radius=10)
        sc=self.sc
        for (c,r) in self.revealed:
            x=int(c*sc); y=int(r*sc); w2=max(1,int(sc)+1); h2=max(1,int(sc)+1)
            pygame.draw.rect(s,(88,82,110,225),(x,y,w2,h2))
        for i,(rx,ry,rw,rh) in enumerate(self._rooms or []):
            if i in self.room_revealed:
                pygame.draw.rect(s,(115,105,140,170),(int(rx*sc),int(ry*sc),int(rw*sc),int(rh*sc)),1)
        self._surf=s; self._dirty=False
    def draw(self,surf,player,enemies,stairs,torches,shop_pos,shop_used):
        if self._dirty or self._surf is None: self._rebuild()
        if self._surf is None: return
        sc=self.sc; x0=W-self.SIZE-self.PAD; y0=H-self.SIZE-self.PAD-44
        surf.blit(self._surf,(x0,y0))
        sx=int(stairs.x/TILE*sc); sy=int(stairs.y/TILE*sc)
        if (int(stairs.x//TILE),int(stairs.y//TILE)) in self.revealed:
            pygame.draw.rect(surf,C["cyan"],(x0+sx-3,y0+sy-3,6,6))
        if shop_pos and not shop_used:
            spx=int(shop_pos.x/TILE*sc); spy=int(shop_pos.y/TILE*sc)
            if (int(shop_pos.x//TILE),int(shop_pos.y//TILE)) in self.revealed:
                pygame.draw.circle(surf,C["gold"],(x0+spx,y0+spy),4)
        for e in enemies:
            ec2=int(e.pos.x//TILE); er2=int(e.pos.y//TILE)
            if (ec2,er2) in self.revealed:
                col_e=(255,50,50) if e.is_boss() else (C["orange"] if e.etype=="elite" else (200,60,60))
                ex2=int(e.pos.x/TILE*sc); ey2=int(e.pos.y/TILE*sc)
                r_dot=3 if e.is_boss() else 2
                pygame.draw.circle(surf,col_e,(x0+ex2,y0+ey2),r_dot)
        ppx=int(player.pos.x/TILE*sc); ppy=int(player.pos.y/TILE*sc)
        pulse_r=3+int(math.sin(pygame.time.get_ticks()/200)*1)
        pygame.draw.circle(surf,C["white"],(x0+ppx,y0+ppy),pulse_r+1)
        pygame.draw.circle(surf,C["blue"],(x0+ppx,y0+ppy),pulse_r)
        fx=player.facing.x; fy=player.facing.y
        if abs(fx)+abs(fy)>.1:
            pygame.draw.line(surf,C["cyan"],(x0+ppx,y0+ppy),(x0+ppx+int(fx*8),y0+ppy+int(fy*8)),1)
        lt=F_SM.render("MAP",True,(130,120,150)); surf.blit(lt,(x0+self.SIZE-28,y0+3))
        total_f=sum(1 for row in self._grid for c in row if c=="floor")
        pct=int(len(self.revealed)/max(1,total_f)*100)
        if pct<100: surf.blit(F_SM.render(f"{pct}%",True,(100,90,120)),(x0+4,y0+3))

# ─── UI SCREENS ───────────────────────────────────────────────────────────────
def shop_screen(surf,player):
    items=random.sample(SHOP_ITEMS,min(4,len(SHOP_ITEMS)))
    waiting=True
    while waiting:
        surf.fill(C["bg"])
        t=F_HUGE.render("SHOP",True,C["gold"]); sub=F_MED.render(f"Coins: {player.coins}  — click to buy",True,C["gray"])
        surf.blit(t,(W//2-t.get_width()//2,40)); surf.blit(sub,(W//2-sub.get_width()//2,105))
        mx,my=pygame.mouse.get_pos()
        for i,item in enumerate(items):
            bx=W//2-440+i*225; by=180; can=player.coins>=item["cost"]
            hov=bx<=mx<=bx+200 and by<=my<=by+180
            bc=item["col"] if hov and can else (60,50,70); bg=(35,28,48) if hov else (22,16,32)
            pygame.draw.rect(surf,bg,(bx,by,200,180),border_radius=10)
            pygame.draw.rect(surf,bc,(bx,by,200,180),2,border_radius=10)
            pygame.draw.circle(surf,item["col"],(bx+100,by+50),28)
            pygame.draw.circle(surf,C["black"],(bx+100,by+50),28,2)
            surf.blit(F_MED.render(item["name"],True,C["white"]),(bx+10,by+90))
            surf.blit(F_SM.render(item["desc"],True,C["gray"]),(bx+10,by+115))
            surf.blit(F_BIG.render(f"{item['cost']}G",True,C["gold"] if can else C["red"]),(bx+10,by+145))
        lr=pygame.Rect(W//2-60,H-80,120,44)
        pygame.draw.rect(surf,(50,40,60) if lr.collidepoint(mx,my) else (30,22,40),lr,border_radius=8)
        pygame.draw.rect(surf,C["gray"],lr,2,border_radius=8)
        surf.blit(F_MED.render("Leave",True,C["white"]),(W//2-25,H-68))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN and ev.key==pygame.K_ESCAPE: waiting=False
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                if lr.collidepoint(mx,my): waiting=False
                for i,item in enumerate(items):
                    bx=W//2-440+i*225; by=180
                    if bx<=mx<=bx+200 and by<=my<=by+180 and player.coins>=item["cost"] and item["kind"]!="sold":
                        player.coins-=item["cost"]; SFX.play("shop_buy"); k=item["kind"]
                        if k=="hp_full": player.hp=player.mhp
                        elif k=="hp_half": player.hp=min(player.mhp,player.hp+player.mhp//2)
                        elif k=="ammo": player.ammo.update({k2:MAX_AMMO[k2] for k2 in MAX_AMMO})
                        elif k=="relic":
                            av=[r for r in RELIC_DEFS if r["id"] not in player.relics]
                            if av:
                                r=random.choice(av); player.relics.append(r["id"])
                                if r["id"]=="glass_cannon": player.mhp=max(20,player.mhp-30);player.hp=min(player.hp,player.mhp)
                        elif k=="weapon":
                            rare=[wk for wk,wv in WEAPONS.items() if wv["rare"] in("rare","epic","legendary")]
                            weapon_pickup_screen(surf,player,random.choice(rare))
                        elif k=="crit": player.crit=min(.85,player.crit+.08)
                        elif k=="max_hp": player.mhp+=25;player.hp=min(player.hp+25,player.mhp)
                        elif k=="bomb": player.ammo["Grenade"]=player.ammo.get("Grenade",0)+3
                        items[i]=dict(kind="sold",name="SOLD",desc="",cost=0,col=C["gray"])
        clock.tick(FPS)

UPGRADES=[
    ("Warrior's Might","+25 MaxHP & Full Heal",lambda p:[setattr(p,"mhp",p.mhp+25),setattr(p,"hp",p.mhp)]),
    ("Critical Edge",  "+12% Crit Chance",     lambda p:setattr(p,"crit",min(.85,p.crit+.12))),
    ("Full Reload",    "Reload all weapons",    lambda p:p.ammo.update({k:MAX_AMMO[k] for k in MAX_AMMO})),
    ("Bloodthirst",    "Heal 40 HP",            lambda p:setattr(p,"hp",min(p.mhp,p.hp+40))),
    ("Spin Cooldown",  "-1s Spin CD",           lambda p:setattr(p,"spin_cd",max(0,p.spin_cd-1))),
    ("Rare Weapon",    "Choose a rare weapon",  None),
]
def level_up_screen(surf,player):
    choices=random.sample(UPGRADES[:5],3)
    if random.random()<.3: choices[random.randint(0,2)]=UPGRADES[5]
    waiting=True
    while waiting:
        dim=pygame.Surface((W,H),pygame.SRCALPHA); dim.fill((0,0,0,175)); surf.blit(dim,(0,0))
        panel=pygame.Rect(W//2-270,H//2-230,540,460)
        pygame.draw.rect(surf,(18,12,26),panel,border_radius=14)
        pygame.draw.rect(surf,C["gold"],panel,3,border_radius=14)
        t=F_HUGE.render("LEVEL UP!",True,C["gold"]); lv=F_MED.render(f"Level {player.level}",True,C["white"])
        surf.blit(t,(W//2-t.get_width()//2,H//2-215)); surf.blit(lv,(W//2-lv.get_width()//2,H//2-165))
        mx,my=pygame.mouse.get_pos()
        for i,(name,desc,_) in enumerate(choices):
            by=H//2-105+i*105; rect=pygame.Rect(W//2-220,by,440,80); hov=rect.collidepoint(mx,my)
            pygame.draw.rect(surf,(42,32,58) if hov else (24,18,34),rect,border_radius=8)
            pygame.draw.rect(surf,C["cyan"] if hov else (55,45,72),rect,2,border_radius=8)
            surf.blit(F_MED.render(name,True,C["white"]),(W//2-205,by+12))
            surf.blit(F_SM.render(desc,True,C["orange"]),(W//2-205,by+40))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                for i,(name,desc,eff) in enumerate(choices):
                    if pygame.Rect(W//2-220,H//2-105+i*105,440,80).collidepoint(mx,my):
                        if eff: eff(player)
                        else:
                            rare=[k for k,v in WEAPONS.items() if v["rare"] in("rare","epic","legendary")]
                            wk=random.choice(rare); player.wkey=wk; player.weapon=WEAPONS[wk]
                            if wk in MAX_AMMO: player.ammo.setdefault(wk,MAX_AMMO[wk])
                        waiting=False
        clock.tick(FPS)

def weapon_pickup_screen(surf,player,new_key):
    chosen=None
    while chosen is None:
        surf.fill(C["bg"]); t=F_BIG.render("Weapon Found!",True,C["gold"]); surf.blit(t,(W//2-t.get_width()//2,60))
        def draw_card(wk,bx,by,label):
            wd=WEAPONS[wk]; rc2=RARITY_COL[wd["rare"]]
            pygame.draw.rect(surf,(28,22,38),(bx,by,290,130),border_radius=8)
            pygame.draw.rect(surf,rc2,(bx,by,290,130),2,border_radius=8)
            surf.blit(F_SM.render(label,True,C["gray"]),(bx+10,by+8))
            surf.blit(F_MED.render(wk,True,rc2),(bx+10,by+30))
            surf.blit(F_SM.render(f"DMG:{wd['dmg']}  SPD:{wd['spd']}  {wd['rare'].upper()}",True,C["white"]),(bx+10,by+60))
            surf.blit(F_SM.render(f"Type: {wd['cls']}",True,wd["col"]),(bx+10,by+85))
        draw_card(player.wkey,50,180,"Current"); draw_card(new_key,W-340,180,"New")
        mx,my=pygame.mouse.get_pos()
        for bx2,label2,col3 in[(W//2-180,"Keep current",C["gray"]),(W//2+20,"Take new",C["green"])]:
            hov=bx2<=mx<=bx2+150 and 360<=my<=410
            pygame.draw.rect(surf,(60,60,60) if hov else (35,35,35),(bx2,360,150,50),border_radius=8)
            pygame.draw.rect(surf,col3,(bx2,360,150,50),2,border_radius=8)
            surf.blit(F_MED.render(label2,True,C["white"]),(bx2+8,373))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.MOUSEBUTTONDOWN:
                mx2,my2=ev.pos
                if W//2-180<=mx2<=W//2-30 and 360<=my2<=410: chosen="keep"
                if W//2+20<=mx2<=W//2+170 and 360<=my2<=410:
                    player.wkey=new_key; player.weapon=WEAPONS[new_key]
                    if new_key in MAX_AMMO: player.ammo.setdefault(new_key,MAX_AMMO[new_key]); chosen="take"
        clock.tick(FPS)

def relic_pickup_screen(surf,player,rid):
    r=RELIC_BY_ID.get(rid);
    if not r: return
    rc=RARITY_COL[r["rare"]]; waiting=True
    while waiting:
        surf.fill(C["bg"]); dim=pygame.Surface((W,H),pygame.SRCALPHA); dim.fill((0,0,0,140)); surf.blit(dim,(0,0))
        panel=pygame.Rect(W//2-180,H//2-120,360,240)
        pygame.draw.rect(surf,(18,12,28),panel,border_radius=14); pygame.draw.rect(surf,rc,panel,3,border_radius=14)
        t=F_BIG.render("Relic Found!",True,rc); surf.blit(t,(W//2-t.get_width()//2,H//2-105))
        pygame.draw.circle(surf,r["col"],(W//2,H//2-30),26); pygame.draw.circle(surf,rc,(W//2,H//2-30),26,2)
        surf.blit(F_MED.render(r["name"],True,C["white"]),(W//2-80,H//2+10))
        surf.blit(F_SM.render(r["desc"],True,C["orange"]),(W//2-90,H//2+38))
        surf.blit(F_SM.render("Click to continue",True,C["gray"]),(W//2-60,H//2+75))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type in(pygame.MOUSEBUTTONDOWN,pygame.KEYDOWN): waiting=False
        clock.tick(FPS)

def pause_menu(surf):
    overlay=pygame.Surface((W,H),pygame.SRCALPHA); overlay.fill((0,0,0,165)); surf.blit(overlay,(0,0))
    options=["Resume","Controls","Quit"]; sel=0; ctrl=False
    CTRL_LIST=[("WASD/Arrows","Move"),("Mouse Left","Attack/Shoot"),("Space hold","Charged Dash"),("Q","Spin Attack"),("E","Energy Shield"),("R","Place Turret"),("1-9","Switch weapon"),("ESC","Pause")]
    while True:
        surf.blit(overlay,(0,0))
        if ctrl:
            panel=pygame.Rect(W//2-200,H//2-180,400,360)
            pygame.draw.rect(surf,(18,12,28),panel,border_radius=14); pygame.draw.rect(surf,C["cyan"],panel,2,border_radius=14)
            t=F_BIG.render("Controls",True,C["cyan"]); surf.blit(t,(W//2-t.get_width()//2,H//2-165))
            for i,(k,a) in enumerate(CTRL_LIST):
                y=H//2-125+i*36; surf.blit(F_SM.render(k,True,C["gold"]),(W//2-185,y)); surf.blit(F_SM.render(a,True,C["white"]),(W//2+10,y))
            back=F_MED.render("Back [ESC]",True,C["gray"]); surf.blit(back,(W//2-back.get_width()//2,H//2+160))
        else:
            t=F_HUGE.render("PAUSED",True,C["white"]); surf.blit(t,(W//2-t.get_width()//2,H//2-160))
            mx,my=pygame.mouse.get_pos()
            for i,opt in enumerate(options):
                by=H//2-60+i*70; rect=pygame.Rect(W//2-120,by,240,52); hov=rect.collidepoint(mx,my)
                pygame.draw.rect(surf,(30,22,42) if hov else (18,12,28),rect,border_radius=8)
                pygame.draw.rect(surf,C["cyan"] if hov else C["gray"],rect,2,border_radius=8)
                ot=F_MED.render(opt,True,C["white"]); surf.blit(ot,(W//2-ot.get_width()//2,by+14))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN:
                if ctrl:
                    if ev.key==pygame.K_ESCAPE: ctrl=False
                else:
                    if ev.key==pygame.K_ESCAPE: return "resume"
                    if ev.key==pygame.K_UP: sel=(sel-1)%3
                    if ev.key==pygame.K_DOWN: sel=(sel+1)%3
                    if ev.key==pygame.K_RETURN:
                        if sel==0: return "resume"
                        if sel==1: ctrl=True
                        if sel==2: sys.exit()
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1 and not ctrl:
                mx2,my2=ev.pos
                for i,opt in enumerate(options):
                    if pygame.Rect(W//2-120,H//2-60+i*70,240,52).collidepoint(mx2,my2):
                        if i==0: return "resume"
                        if i==1: ctrl=True
                        if i==2: sys.exit()
        clock.tick(FPS)

def meta_screen(surf,meta,score,floor):
    earned=score//50; meta["coins_total"]=meta.get("coins_total",0)+earned
    meta["runs"]=meta.get("runs",0)+1; meta["best_floor"]=max(meta.get("best_floor",0),floor)
    meta["best_score"]=max(meta.get("best_score",0),score); save_meta(meta)
    waiting=True
    while waiting:
        surf.fill(C["bg"])
        surf.blit(F_HUGE.render("RUN OVER",True,C["red"]),(W//2-120,40))
        for i,(label,val) in enumerate([(f"Score: {score}",C["gold"]),(f"Floor reached: {floor}",C["cyan"]),(f"Coins earned: +{earned}G",C["gold"]),(f"Total coins: {meta['coins_total']}G",C["white"]),(f"Best floor: {meta['best_floor']}",C["green"])]):
            surf.blit(F_MED.render(label,True,val),(80,120+i*32))
        mx,my=pygame.mouse.get_pos()
        surf.blit(F_BIG.render("Permanent Upgrades",True,C["gold"]),(W//2+20,110))
        for i,u in enumerate(META_UPGRADES):
            lv=meta["upgrades"].get(u["id"],0); by2=150+i*52; bx2=W//2+20; maxed=lv>=u["max_level"]
            can=meta["coins_total"]>=u["cost"] and not maxed; hov=bx2<=mx<=bx2+380 and by2<=my<=by2+44
            pygame.draw.rect(surf,(40,32,55) if hov and can else (20,15,28),(bx2,by2,380,44),border_radius=6)
            col_b=C["gold"] if can else (C["gray"] if not maxed else C["green"])
            pygame.draw.rect(surf,col_b,(bx2,by2,380,44),2,border_radius=6)
            lv_str="MAX" if maxed else f"Lv{lv}/{u[chr(109)+chr(97)+chr(120)+chr(95)+chr(108)+chr(101)+chr(118)+chr(101)+chr(108)]}"
            surf.blit(F_SM.render(f"{u[chr(110)+chr(97)+chr(109)+chr(101)]} ({lv_str})",True,C["white"]),(bx2+8,by2+6))
            surf.blit(F_SM.render(u["desc"],True,C["gray"]),(bx2+8,by2+24))
            surf.blit(F_SM.render("MAXED" if maxed else f"{u['cost']}G",True,col_b),(bx2+315,by2+14))
        for bx3,label3 in[(W//2-160,"Play Again [R]"),(W//2+20,"Quit [ESC]")]:
            hr=pygame.Rect(bx3,H-70,150,46); hv2=hr.collidepoint(mx,my)
            pygame.draw.rect(surf,(50,40,60) if hv2 else (30,22,40),hr,border_radius=8)
            pygame.draw.rect(surf,C["cyan"] if "Play" in label3 else C["gray"],hr,2,border_radius=8)
            surf.blit(F_MED.render(label3,True,C["white"]),(bx3+8,H-56))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                for i,u in enumerate(META_UPGRADES):
                    by2=150+i*52; bx2=W//2+20; lv=meta["upgrades"].get(u["id"],0)
                    if bx2<=mx<=bx2+380 and by2<=my<=by2+44 and meta["coins_total"]>=u["cost"] and lv<u["max_level"]:
                        meta["coins_total"]-=u["cost"]; meta["upgrades"][u["id"]]=lv+1; save_meta(meta); SFX.play("shop_buy")
                if pygame.Rect(W//2-160,H-70,150,46).collidepoint(mx,my): waiting=False; return "restart"
                if pygame.Rect(W//2+20,H-70,150,46).collidepoint(mx,my): sys.exit()
            if ev.type==pygame.KEYDOWN:
                if ev.key==pygame.K_r: waiting=False; return "restart"
                if ev.key==pygame.K_ESCAPE: sys.exit()
        clock.tick(FPS)
    return "restart"

def draw_relic_hud(surf,player):
    if not player.relics: return
    total=len(player.relics); start_x=W//2-total*22
    for i,rid in enumerate(player.relics):
        r=RELIC_BY_ID.get(rid,{}); rc5=RARITY_COL.get(r.get("rare","common"),(180,180,180))
        cx=start_x+i*44; cy=H-22
        pygame.draw.circle(surf,r.get("col",C["gray"]),(cx,cy),14)
        pygame.draw.circle(surf,rc5,(cx,cy),14,2)
        lt=F_SM.render(r.get("name","?")[:4],True,C["white"]); lt.set_alpha(200)
        surf.blit(lt,(cx-lt.get_width()//2,cy+16))

def explode(pos,dmg,radius,entities,ps,cam):
    ps.emit(pos.x,pos.y,35,C["orange"],280,.5,5); ps.emit(pos.x,pos.y,20,C["yellow"],160,.3,3); cam.hit(10,.25)
    for e in entities:
        if hasattr(e,"pos") and pos.distance_to(e.pos)<radius: e.hit(dmg,pos,600)

# ─── CHARACTER CLASSES ────────────────────────────────────────────────────────
CLASSES = {
    "Warrior": dict(
        col      = C["orange"],
        desc     = "Tank melee bruiser. High HP, powerful Q spin, bonus armor.",
        mhp      = 180,
        crit     = 0.10,
        speed    = 1.0,
        start_wep= "Iron Axe",
        dash_cd  = 0.7,
        max_dashes=1,
        # Skill overrides
        q_desc   = "WHIRLWIND – Huge spin, 180° arc, lifesteal 10%",
        e_desc   = "IRON SKIN – Reduce all dmg by 60% for 4s",
        r_desc   = "WAR CRY – +50% dmg for 5s, nearby enemies stunned",
        passive  = "Thick Hide: take 15% less damage from all sources",
        sprite_col= (200,100,30),
        anim     = None,  # set after sprite creation
        icon_pix = [
            "..OOOO..","OYYYYYO.","OYWWWYO.","OYYYYYO.","..OXXO..","AAAAAAA.","ASSSSAA.","ASSSSAA.","..AA.AA.",
        ],
        icon_col = {'O':(200,100,30),'Y':(230,170,80),'W':(15,15,15),'X':(40,20,5),'A':(150,60,10),'S':(180,120,60)},
    ),
    "Mage": dict(
        col      = C["purple"],
        desc     = "Arcane glass cannon. Low HP, massive skill power, mana orbs.",
        mhp      = 80,
        crit     = 0.25,
        speed    = 0.95,
        start_wep= "Magic Orb",
        dash_cd  = 1.1,
        max_dashes=1,
        q_desc   = "NOVA – Giant magic explosion, 150 dmg, 200px radius",
        e_desc   = "BLINK – Instant teleport to cursor, invuln 0.5s",
        r_desc   = "METEOR – Call 5 meteors on cursor area, each 80 dmg",
        passive  = "Arcane Mastery: spells cost no cooldown every 8s",
        sprite_col= (160,80,220),
        anim     = None,
        icon_pix = [
            "..PPPP..","PSSSSP.","PSOSOSP","PSSSSP.","..PMMM..","RRRRRR.","RSSSRR.","RSSSRR.","..RR.RR.",
        ],
        icon_col = {'P':(120,40,200),'S':(220,190,240),'O':(15,15,15),'M':(80,20,140),'R':(160,80,220)},
    ),
    "Rogue": dict(
        col      = C["cyan"],
        desc     = "Agile assassin. Double dash, highest crit, backstab bonus.",
        mhp      = 100,
        crit     = 0.35,
        speed    = 1.3,
        start_wep= "Bow",
        dash_cd  = 0.5,
        max_dashes=2,
        q_desc   = "SHADOWSTRIKE – Dash + 3x crit multiplier on next hit",
        e_desc   = "SMOKE BOMB – Invuln 2s + leave poison cloud",
        r_desc   = "FAN OF KNIVES – 12 daggers in all directions, pierce",
        passive  = "Backstab: +80% dmg when attacking from behind",
        sprite_col= (40,200,180),
        anim     = None,
        icon_pix = [
            "..CCCC..","CSSSSC.","CSOSOC.","CSSSSC.","..CTTC..","DDDDDD.","DSSSDD.","DSSSDD.","..DD.DD.",
        ],
        icon_col = {'C':(30,180,160),'S':(200,230,230),'O':(10,10,10),'T':(20,140,120),'D':(20,110,90)},
    ),
}

# Build class icon sprites
for cname, cd in CLASSES.items():
    cd["icon_surf"] = _spr(cd["icon_pix"], cd["icon_col"], 4)

# ─── CHARACTER SELECT SCREEN ──────────────────────────────────────────────────
def char_select_screen(surf):
    """Full character select screen. Returns chosen class name."""
    sel       = 0
    class_list= list(CLASSES.keys())
    t_anim    = 0.0
    clock2    = pygame.time.Clock()

    # Particle bg
    bg_pts = [[random.uniform(0,W), random.uniform(0,H),
               random.uniform(-20,20), random.uniform(-20,20),
               random.uniform(80,200)] for _ in range(60)]

    while True:
        dt2 = clock2.tick(60)/1000.0
        t_anim += dt2

        # Update bg particles
        for p in bg_pts:
            p[0]=(p[0]+p[2]*dt2)%W; p[1]=(p[1]+p[3]*dt2)%H

        # ── DRAW ──
        surf.fill((6,4,10))

        # Bg particles
        for p in bg_pts:
            a = int(40 + math.sin(t_anim*1.5+p[4])*30)
            s2 = pygame.Surface((4,4),pygame.SRCALPHA)
            pygame.draw.circle(s2,(120,80,200,a),(2,2),2)
            surf.blit(s2,(int(p[0]),int(p[1])))

        # Title
        title_col = (
            int(180+math.sin(t_anim*2.2)*60),
            int(100+math.sin(t_anim*1.8+1)*60),
            int(220+math.sin(t_anim*1.4+2)*35),
        )
        t1 = F_HUGE.render("SOUL DESCENT", True, title_col)
        surf.blit(t1,(W//2-t1.get_width()//2, 30))
        t2 = F_MED.render("Choose Your Class", True, C["gray"])
        surf.blit(t2,(W//2-t2.get_width()//2, 96))

        mx,my = pygame.mouse.get_pos()

        # 3 class cards
        card_w, card_h = 280, 460
        total_w = card_w*3 + 40*2
        start_x = W//2 - total_w//2

        for i, cname in enumerate(class_list):
            cd  = CLASSES[cname]
            cx  = start_x + i*(card_w+40)
            cy  = 130
            hov = cx<=mx<=cx+card_w and cy<=my<=cy+card_h
            is_sel = (i==sel)

            # Card bg
            border_col = cd["col"] if is_sel else ((200,200,220) if hov else (50,45,65))
            bg_col     = (30,24,44) if is_sel else ((20,16,30) if hov else (14,10,22))
            pygame.draw.rect(surf, bg_col,    (cx,cy,card_w,card_h), border_radius=14)
            pygame.draw.rect(surf, border_col,(cx,cy,card_w,card_h), 3 if is_sel else 1, border_radius=14)

            # Selection glow
            if is_sel:
                glow_a = int(30+math.sin(t_anim*4)*20)
                gs = pygame.Surface((card_w+20,card_h+20),pygame.SRCALPHA)
                pygame.draw.rect(gs,(*cd["col"],glow_a),(0,0,card_w+20,card_h+20),border_radius=16)
                surf.blit(gs,(cx-10,cy-10))

            # Class icon (big, centered)
            icon = cd["icon_surf"]
            bob  = math.sin(t_anim*2.5 + i*2.1) * (5 if is_sel else 2)
            ix   = cx + card_w//2 - icon.get_width()//2
            iy   = cy + 24 + int(bob)
            surf.blit(icon,(ix,iy))

            # Class name
            cn_col = cd["col"] if is_sel else C["white"]
            nt = F_BIG.render(cname, True, cn_col)
            surf.blit(nt,(cx+card_w//2-nt.get_width()//2, cy+130))

            # Stats bars
            stats_y = cy+168
            stat_items = [
                ("HP",    cd["mhp"]/200),
                ("SPD",   cd["speed"]/1.4),
                ("CRIT",  cd["crit"]/.4),
                ("DASH",  1-cd["dash_cd"]/1.2),
            ]
            for j,(stat,ratio) in enumerate(stat_items):
                sy = stats_y + j*28
                surf.blit(F_SM.render(stat,True,C["gray"]),(cx+14,sy+2))
                pygame.draw.rect(surf,(25,20,35),(cx+60,sy+2,180,16),border_radius=4)
                fw2 = int(180*max(0,min(1,ratio)))
                if fw2>0:
                    bar_col = cd["col"] if is_sel else (100,90,120)
                    pygame.draw.rect(surf,bar_col,(cx+60,sy+2,fw2,16),border_radius=4)
                pygame.draw.rect(surf,(60,55,75),(cx+60,sy+2,180,16),1,border_radius=4)

            # Passive
            surf.blit(F_SM.render("PASSIVE:",True,(150,150,170)),(cx+14,cy+284))
            # Word-wrap passive
            words = cd["passive"].split(" ")
            line = ""; lines2 = []
            for w3 in words:
                test = line+" "+w3 if line else w3
                if F_SM.size(test)[0] > card_w-28: lines2.append(line); line=w3
                else: line=test
            if line: lines2.append(line)
            for li,ln in enumerate(lines2[:2]):
                surf.blit(F_SM.render(ln,True,C["lime"]),(cx+14,cy+302+li*17))

            # Skills
            skills_y = cy+345
            for si,(key,desc) in enumerate([("Q",cd["q_desc"]),("E",cd["e_desc"]),("R",cd["r_desc"])]):
                sy2 = skills_y + si*34
                pygame.draw.rect(surf,(30,25,45),(cx+10,sy2,card_w-20,28),border_radius=5)
                pygame.draw.rect(surf,cd["col"] if is_sel else (50,45,65),(cx+10,sy2,card_w-20,28),1,border_radius=5)
                kl = F_SM.render(f"[{key}]",True,cd["col"]); surf.blit(kl,(cx+16,sy2+7))
                # Truncate desc
                dl = F_SM.render(desc[:32]+"…" if len(desc)>32 else desc,True,C["white"])
                surf.blit(dl,(cx+44,sy2+7))

            # Hover = select
            if hov: sel=i

        # Bottom instructions
        sel_name = class_list[sel]
        sel_cd   = CLASSES[sel_name]
        bx4 = W//2; by4 = H-70
        # Confirm button
        btn_r = pygame.Rect(bx4-110,by4,220,50)
        btn_hov = btn_r.collidepoint(mx,my)
        pygame.draw.rect(surf,sel_cd["col"] if btn_hov else (35,28,48),btn_r,border_radius=10)
        pygame.draw.rect(surf,sel_cd["col"],btn_r,2,border_radius=10)
        bt = F_BIG.render(f"Play as {sel_name}",True,C["white"])
        surf.blit(bt,(bx4-bt.get_width()//2,by4+10))

        pygame.display.flip()

        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN:
                if ev.key==pygame.K_ESCAPE: sys.exit()
                if ev.key==pygame.K_LEFT:  sel=(sel-1)%3
                if ev.key==pygame.K_RIGHT: sel=(sel+1)%3
                if ev.key in(pygame.K_RETURN,pygame.K_SPACE): return class_list[sel]
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                if btn_r.collidepoint(mx,my): return class_list[sel]
                for i,cname in enumerate(class_list):
                    cx2=start_x+i*(card_w+40); cy2=130
                    if cx2<=mx<=cx2+card_w and cy2<=my<=cy2+card_h:
                        if i==sel: return cname
                        sel=i

def apply_class(player, class_name):
    """Apply class bonuses/overrides to player after creation."""
    cd = CLASSES[class_name]
    player.class_name = class_name
    player.mhp  = cd["mhp"]
    player.hp   = player.mhp
    player.crit = cd["crit"]
    player._class_speed = cd["speed"]
    player.dash_cd_base = cd["dash_cd"]
    player.max_dashes   = cd["max_dashes"]
    player.dashes_left  = cd["max_dashes"]
    player.wkey   = cd["start_wep"]
    player.weapon = WEAPONS[cd["start_wep"]]
    # Class passive flags
    player._passive_dmg_reduce = 0.15 if class_name=="Warrior" else 0.0
    player._backstab           = (class_name=="Rogue")
    player._free_skill_t       = 0.0
    player._free_skill_cd      = 8.0 if class_name=="Mage" else 999
    player._shadow_strike_ready= False
    player._shadow_strike_next = False
    # Apply meta bonuses on top
    upg = META.get("upgrades",{})
    player.mhp += upg.get("hp_up",0)*15; player.hp=player.mhp
    player.crit = min(0.85, player.crit + upg.get("crit_up",0)*0.05)
    player._meta_dmg = 1.0 + upg.get("dmg_up",0)*0.08

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    META.update(load_meta())

    # ── LOBBY ──
    net_mode, host_ip, player_name = lobby_screen(screen)

    # ── CHARACTER SELECT ──
    chosen_class = char_select_screen(screen)
    SFX.play("levelup")

    # ── MULTIPLAYER SETUP ──
    net_host   = None
    net_client = None
    all_peers  = []   # list of {pid, name, class}
    my_pid     = 0
    mp_seed    = random.randint(0,999999)

    if net_mode == "host":
        net_host = NetHost()
        mp_seed  = net_host.seed
        pygame.display.set_caption(f"Soul Descent — Host ({player_name})")
        all_peers = waiting_room(screen, net_host, player_name, chosen_class)
        my_pid = 0
    elif net_mode == "join":
        net_client = NetClient(host_ip, player_name, chosen_class)
        pygame.display.set_caption(f"Soul Descent — {player_name}")
        ok = joining_screen(screen, net_client)
        if not ok:
            if net_client: net_client.stop()
            return main()
        my_pid  = net_client.pid
        mp_seed = net_client.seed
        all_peers = [{"pid":0,"name":"Host","class":"Warrior"}] + net_client.peers
    else:
        all_peers = [{"pid":0,"name":player_name,"class":chosen_class}]
        my_pid = 0

    # Use shared seed for deterministic map
    random.seed(mp_seed)

    floor=1; kills=0; score=0
    grid,rooms,rtypes,torches,p_start,stairs,theme=gen_map(floor)
    player=Player(p_start.x,p_start.y)
    apply_class(player, chosen_class)
    cam=Camera(); cam.pos=Vector2(player.pos)
    ps=PS(); enemies=spawn_enemies(rooms,rtypes,floor); objs,loots=spawn_objs(rooms,rtypes,floor)
    turrets=[]; projs=[]; floor_surf=build_floor(grid,theme)
    shop_pos=None; shop_used=False
    for room,rt in zip(rooms,rtypes):
        if rt=="shop": shop_pos=Vector2((room[0]+room[2]//2)*TILE,(room[1]+room[3]//2)*TILE); break

    ui_hp=float(player.hp); ui_xp=0.0; torch_t=0.0; slow_t=0.0
    combo=ComboSystem(); death_anims=[]; blood=BloodDecal()
    room_clear_fx=RoomClearFX(); room_was_clear=len(enemies)==0
    minimap=Minimap(); minimap.new_floor(grid,rooms)
    music_started=False; music_mode="normal"
    env_fx=EnvFX(); env_fx.set_theme(theme)
    wtrail=WeaponTrail()
    hud_t=0.0

    # Multiplayer state
    remote_players = {}  # pid → {pos, hp, mhp, class, name, facing, dead, revive_t}
    for p in all_peers:
        if p["pid"] != my_pid:
            remote_players[p["pid"]] = {
                "pos": Vector2(p_start), "hp": 100, "mhp": 100,
                "class": p.get("class","Warrior"), "name": p.get("name","?"),
                "facing": Vector2(1,0), "dead": False, "revive_t": 0.0,
                "anim_t": 0.0, "vel": Vector2(0,0),
            }
    net_tick_t  = 0.0
    NET_SEND_HZ = 1/20   # send 20x/sec
    chat_msgs   = []     # [(pid, msg, timer)]
    chat_input  = ""
    chat_open   = False
    peer_name_map = {p["pid"]:p["name"] for p in all_peers}

    LS=.5; lw,lh=int(W*LS),int(H*LS); lmap=pygame.Surface((lw,lh))
    lp_s=pygame.transform.scale(LT_PLAYER,(int(460*LS),int(460*LS)))
    lt_s=pygame.transform.scale(LT_TORCH,(int(540*LS),int(540*LS)))
    lj_s=pygame.transform.scale(LT_PROJ,(int(200*LS),int(200*LS)))
    lb_s=pygame.transform.scale(LT_BOSS,(int(640*LS),int(640*LS)))
    ll_s=pygame.transform.scale(LT_LAVA,(int(400*LS),int(400*LS)))

    def add_slow(dur): nonlocal slow_t; slow_t=max(slow_t,dur)

    def new_floor():
        nonlocal floor,grid,rooms,rtypes,torches,p_start,stairs,enemies,objs,loots,projs,turrets,floor_surf,shop_pos,shop_used,room_was_clear,theme
        floor+=1; grid,rooms,rtypes,torches,p_start,stairs,theme=gen_map(floor)
        player.pos=Vector2(p_start); player.hp=min(player.mhp,player.hp+35)
        floor_surf=build_floor(grid,theme); enemies=spawn_enemies(rooms,rtypes,floor)
        objs,loots=spawn_objs(rooms,rtypes,floor); projs.clear(); turrets.clear()
        shop_pos=None; shop_used=False; room_was_clear=len(enemies)==0
        for room,rt in zip(rooms,rtypes):
            if rt=="shop": shop_pos=Vector2((room[0]+room[2]//2)*TILE,(room[1]+room[3]//2)*TILE); break
        minimap.new_floor(grid,rooms)
        env_fx.set_theme(theme)

    running=True
    while running:
        dt=min(clock.tick(FPS)/1000.0,.05)
        if slow_t>0: slow_t-=dt; dt*=.28
        keys=pygame.key.get_pressed()
        mpos=pygame.mouse.get_pos()
        mw=cam.pos-Vector2(W/2,H/2)+Vector2(mpos)
        torch_t+=dt

        for ev in pygame.event.get():
            if ev.type==pygame.QUIT: sys.exit()
            if ev.type==pygame.KEYDOWN:
                if chat_open:
                    if ev.key==pygame.K_RETURN:
                        if chat_input.strip():
                            msg = chat_input.strip()
                            chat_msgs.append([f"You: {msg}", 5.0])
                            if net_client: net_client.send(NetMsg.CHAT,{"msg":msg})
                            elif net_host:
                                with net_host._lock:
                                    for addr in net_host.peers:
                                        net_host._send(addr,NetMsg.CHAT,{"pid":0,"msg":msg})
                        chat_input=""; chat_open=False
                    elif ev.key==pygame.K_ESCAPE: chat_input=""; chat_open=False
                    elif ev.key==pygame.K_BACKSPACE: chat_input=chat_input[:-1]
                    else:
                        if ev.unicode.isprintable() and len(chat_input)<60:
                            chat_input+=ev.unicode
                else:
                    if ev.key==pygame.K_ESCAPE:
                        res=pause_menu(screen)
                        if res!="resume": sys.exit()
                    elif ev.key==pygame.K_t: chat_open=True
            if ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                player.fire(projs,ps,cam)
            if ev.type==pygame.KEYDOWN:
                wlist=list(WEAPONS.keys())
                for i,k in enumerate(range(pygame.K_1,pygame.K_9)):
                    if ev.key==k and i<len(wlist):
                        nk=wlist[i]
                        if WEAPONS[nk]["ammo"]==-1 or nk in player.ammo: player.wkey=nk; player.weapon=WEAPONS[nk]
            if ev.type==pygame.USEREVENT+1:
                MUSIC.try_start_pending(); pygame.time.set_timer(pygame.USEREVENT+1,0)

        player.update(dt,grid,keys,mw,ps,cam,enemies,projs,turrets)
        if player.swinging: player.do_melee_hits(enemies,ps,cam,add_slow)
        player.update_statuses(dt,ps)
        cam.update(dt,player.pos,player.facing); ps.update(dt)

        # ── NETWORK TICK ──
        net_tick_t += dt
        if net_tick_t >= NET_SEND_HZ:
            net_tick_t = 0.0
            # Build local input snapshot
            my_state = {
                "pos":   (player.pos.x, player.pos.y),
                "hp":    player.hp, "mhp": player.mhp,
                "facing":(player.facing.x, player.facing.y),
                "vel":   (player.vel.x, player.vel.y),
                "dead":  player.hp <= 0,
                "class": chosen_class,
            }
            if net_client:
                net_client.send_input(my_state)
                # Receive state from host
                state = net_client.get_state()
                if state and "players" in state:
                    for pid, pdata in state["players"].items():
                        if pid != my_pid and pid in remote_players:
                            rp = remote_players[pid]
                            rp["pos"]    = Vector2(pdata["pos"])
                            rp["hp"]     = pdata["hp"]
                            rp["mhp"]    = pdata["mhp"]
                            rp["facing"] = Vector2(pdata["facing"])
                            rp["vel"]    = Vector2(pdata["vel"])
                            rp["dead"]   = pdata["dead"]
                            rp["anim_t"] += dt
                # Incoming chat
                with net_client._lock:
                    for pid, msg in net_client.chat:
                        name = peer_name_map.get(pid, f"P{pid}")
                        chat_msgs.append([f"{name}: {msg}", 5.0])
                    net_client.chat.clear()

            elif net_host:
                # Host: collect all inputs, broadcast state
                inputs = net_host.get_inputs()
                # Update remote players from their inputs
                for pid, inp in inputs.items():
                    if pid in remote_players:
                        rp = remote_players[pid]
                        rp["pos"]    = Vector2(inp["pos"])
                        rp["hp"]     = inp["hp"]
                        rp["mhp"]    = inp["mhp"]
                        rp["facing"] = Vector2(inp["facing"])
                        rp["vel"]    = Vector2(inp["vel"])
                        rp["dead"]   = inp["dead"]
                        rp["anim_t"] += dt
                # Broadcast combined state
                all_states = {my_pid: my_state}
                for pid, rp in remote_players.items():
                    all_states[pid] = {
                        "pos": (rp["pos"].x, rp["pos"].y),
                        "hp": rp["hp"], "mhp": rp["mhp"],
                        "facing": (rp["facing"].x, rp["facing"].y),
                        "vel": (rp["vel"].x, rp["vel"].y),
                        "dead": rp["dead"],
                    }
                net_host.broadcast_state({"players": all_states})
                # Incoming chat
                with net_host._lock:
                    for pid, msg in net_host.chat:
                        name = peer_name_map.get(pid, f"P{pid}")
                        chat_msgs.append([f"{name}: {msg}", 5.0])
                    net_host.chat.clear()

        # Chat timer decay
        chat_msgs = [[m, t-dt] for m,t in chat_msgs if t-dt > 0]
        combo.update(dt); blood.update(dt); room_clear_fx.update(dt)
        env_fx.update(dt,cam,grid); wtrail.update(dt); hud_t+=dt
        for da in death_anims[:]:
            da.update(dt)
            if da.done: death_anims.remove(da)
        minimap.update(player.pos,rooms)
        if player._coin_magnet:
            for l in loots:
                if l.kind=="coin" and player.pos.distance_to(l.pos)<120:
                    d2=player.pos-l.pos
                    if d2.length()>0: l.pos+=d2.normalize()*200*dt
        if shop_pos and not shop_used and player.pos.distance_to(shop_pos)<TILE:
            shop_used=True; shop_screen(screen,player)
        # Music
        if not music_started and MUSIC.is_ready(): MUSIC.start(); music_started=True
        has_boss=any(e.is_boss() for e in enemies)
        nm="boss" if has_boss else "normal"
        if nm!=music_mode: music_mode=nm; MUSIC.set_mode(nm)

        # Turrets
        for tu in turrets[:]:
            tu.update(dt,enemies,projs,ps)
            if tu.hp<=0: turrets.remove(tu)

        # Projectiles
        for p in projs[:]:
            tgts=enemies if p.owner in("player","turret") else []
            p.update(dt,tgts)
            r2,c2=int(p.pos.y//TILE),int(p.pos.x//TILE)
            in_wall=0<=r2<len(grid) and 0<=c2<len(grid[0]) and grid[r2][c2]=="wall"
            if p.life<=0 or in_wall:
                if p.explosive:
                    tg=enemies if p.owner in("player","turret") else enemies+[player]
                    explode(p.pos,p.dmg*3,90,tg,ps,cam); SFX.play("explode")
                else: ps.emit(p.pos.x,p.pos.y,6,p.col,120,.18)
                if p in projs: projs.remove(p); continue
            if p.owner=="enemy" and p.pos.distance_to(player.pos)<player.rad+p.rad:
                if player.invuln<=0 and player.shield_t<=0:
                    player.hit(p.dmg,p.pos,350); cam.hit(5,.18)
                    ps.emit(player.pos.x,player.pos.y,10,C["red"],150); blood.splat(player.pos.x,player.pos.y,(180,20,20),3)
                if p in projs: projs.remove(p); continue
            if p.owner in("player","turret"):
                hit_any=False
                for e in enemies[:]:
                    if id(e) in p.hit_enemies: continue
                    if p.pos.distance_to(e.pos)<e.rad+p.rad:
                        e.hit(p.dmg,p.pos,400)
                        dv3=(e.pos-p.pos); dv3=dv3.normalize() if dv3.length()>0 else None
                        ps.emit(e.pos.x,e.pos.y,10,p.col,180,dv=dv3); ps.splat(e.pos.x,e.pos.y,e.col,2)
                        p.hit_enemies.add(id(e)); hit_any=True
                        SFX.play("boss_hit" if e.is_boss() else "hit")
                        if getattr(p,"status_on_hit",None): e.apply_status(p.status_on_hit)
                        if "bloodstone" in player.relics: player.hp=min(player.mhp,player.hp+int(p.dmg*.05))
                        if p.explosive:
                            explode(p.pos,p.dmg*2,80,enemies,ps,cam); SFX.play("explode")
                            if p in projs: projs.remove(p); break
                if hit_any and not p.pierce:
                    if p in projs: projs.remove(p)
            if p.trail_t>.06:
                ps.emit(p.pos.x,p.pos.y,2,p.col,20,.2,2); p.trail_t=0

        # Enemies
        now_clear=True
        for e in enemies[:]:
            e.update_statuses(dt,ps); e.ai(dt,player,grid,projs,ps)
            if e.hp>0: now_clear=False
            if e.hp<=0:
                ps.emit(e.pos.x,e.pos.y,30,e.col,240); ps.emit(e.pos.x,e.pos.y,12,C["white"],110,.3)
                death_anims.append(DeathAnim(e.pos.x,e.pos.y,e.col,e.rad,e.etype))
                blood.splat(e.pos.x,e.pos.y,e.col,n=8 if e.is_boss() else 4)
                if e.is_boss(): slow_t=1.4; cam.hit(16,.55); SFX.play("boss_die")
                else: SFX.play("enemy_die")
                mult=combo.kill(ps,e.pos.x,e.pos.y)
                if combo.count in(5,8,12): slow_t=max(slow_t,.45); cam.hit(6,.15)
                player.xp+=e.xpv; kills+=1; score+=int(e.xpv*10*mult)
                if "bomb_belt" in player.relics and random.random()<.2: explode(e.pos,30,80,enemies,ps,cam)
                if "vampiric" in player.relics: player.hp=min(player.mhp,player.hp+5)
                if "soul_link" in player.relics: player.coins+=max(1,e.xpv//8)
                r3=random.random()
                if r3<.25: loots.append(Loot(e.pos.x,e.pos.y,"coin",val=random.randint(1,5)))
                elif r3<.40: loots.append(Loot(e.pos.x,e.pos.y,"hp",val=15))
                elif r3<.46 and e.etype in("elite","knight","shielder","warlock"):
                    rare=[k for k,v in WEAPONS.items() if v["rare"] in("rare","epic","legendary")]
                    loots.append(Loot(e.pos.x,e.pos.y,"weapon",wkey=random.choice(rare)))
                elif e.is_boss():
                    av=[r4 for r4 in RELIC_DEFS if r4["id"] not in player.relics]
                    if av: loots.append(Loot(e.pos.x,e.pos.y,"relic",wkey=random.choice(av)["id"]))
                enemies.remove(e)
                if player.xp>=player.xp_next:
                    player.level+=1; player.xp-=player.xp_next; player.xp_next=int(player.xp_next*1.55)
                    player.hp=player.mhp; SFX.play("levelup"); level_up_screen(screen,player)
        if now_clear and not room_was_clear: room_clear_fx.trigger(ps,player.pos.x,player.pos.y)
        room_was_clear=now_clear

        # Objects
        for o in objs[:]:
            o.update(dt)
            if o.hp<=0:
                ps.emit(o.pos.x,o.pos.y,20,o.col,180,.4,4)
                if o.explodes: explode(o.pos,40,100,enemies,ps,cam)
                if random.random()<o.drop:
                    loots.append(Loot(o.pos.x,o.pos.y,"coin" if random.random()<.6 else "hp",val=random.randint(1,4) if random.random()<.6 else 10))
                o.dead=True
            if player.swinging and player.pos.distance_to(o.pos)<62 and not o.dead: o.hit(player.stats()["dmg"]//2)
            for p in projs[:]:
                if p.owner=="player" and p.pos.distance_to(o.pos)<22 and not o.dead:
                    o.hit(p.dmg//2)
                    if not p.pierce and p in projs: projs.remove(p)
        objs=[o for o in objs if not o.dead]

        # Loot
        for l in loots[:]:
            l.update(dt)
            if player.pos.distance_to(l.pos)<22:
                if l.kind=="coin": player.coins+=l.val; score+=l.val*100; ps.emit_txt(l.pos.x,l.pos.y-10,f"+{l.val}G",C["gold"]); SFX.play("pickup")
                elif l.kind=="hp": player.hp=min(player.mhp,player.hp+l.val); ps.emit_txt(l.pos.x,l.pos.y-10,f"+{l.val}HP",C["green"]); SFX.play("pickup")
                elif l.kind=="weapon" and l.wkey: weapon_pickup_screen(screen,player,l.wkey)
                elif l.kind=="relic" and l.wkey:
                    if l.wkey not in player.relics:
                        player.relics.append(l.wkey)
                        if l.wkey=="glass_cannon": player.mhp=max(20,player.mhp-30);player.hp=min(player.hp,player.mhp)
                        relic_pickup_screen(screen,player,l.wkey)
                l.collected=True
        loots=[l for l in loots if not l.collected]

        if player.pos.distance_to(stairs)<TILE and not enemies: new_floor()

        # ── DRAW ──
        screen.fill(C["bg"])
        screen.blit(floor_surf,cam.apply(Vector2(0,0)))
        env_fx.draw(screen,cam)
        blood.draw(screen,cam)
        sp=cam.apply(stairs); rr2=pygame.Rect(int(sp.x-18),int(sp.y-18),36,36)
        pygame.draw.rect(screen,(0,100,100),rr2,border_radius=4); pygame.draw.rect(screen,C["cyan"],rr2,2,border_radius=4)
        if not enemies: ht=F_SM.render("DESCEND >>",True,C["cyan"]); screen.blit(ht,(int(sp.x)-ht.get_width()//2,int(sp.y)-30))
        if shop_pos and not shop_used:
            shp=cam.apply(shop_pos)
            pygame.draw.rect(screen,(60,45,0),(int(shp.x-18),int(shp.y-18),36,36),border_radius=4)
            pygame.draw.rect(screen,C["gold"],(int(shp.x-18),int(shp.y-18),36,36),2,border_radius=4)
            surf2=F_SM.render("SHOP",True,C["gold"]); screen.blit(surf2,(int(shp.x)-surf2.get_width()//2,int(shp.y)-28))
        for o in objs: o.draw(screen,cam)
        for l in loots: l.draw(screen,cam)
        for tu in turrets: tu.draw(screen,cam)
        dl=[(player.pos.y,player)]+[(e.pos.y,e) for e in enemies]+[(p.pos.y,p) for p in projs]
        dl.sort(key=lambda x:x[0])
        for _,obj in dl: obj.draw(screen,cam)
        # Weapon trail glows (drawn on top of sprites)
        for p2 in projs:
            if p2.owner in("player","turret"):
                wtrail.draw_for_proj(screen,cam,p2.pos,p2.col.__class__.__name__,p2.col)
        ps.draw(screen,cam)
        for da in death_anims: da.draw(screen,cam)

        # Draw remote players
        for pid, rp in remote_players.items():
            if rp["dead"]: continue
            rp_screen = cam.apply(rp["pos"])
            # Shadow
            pygame.draw.ellipse(screen,(0,0,0),(int(rp_screen.x-12),int(rp_screen.y+10),24,8))
            # Sprite (class-colored)
            rp_anim = CLASS_ANIMS.get(rp["class"], ANIM_PLAYER)
            rp_frame = get_anim_frame(rp_anim, rp["anim_t"], rp["vel"])
            flip_rp  = rp["facing"].x < -0.1
            draw_sprite(screen, rp_frame, rp_screen.x, rp_screen.y, flip_x=flip_rp)
            # Color ring
            pid_col = COLORS_MP[pid % len(COLORS_MP)]
            pr = pygame.Surface((32,32),pygame.SRCALPHA)
            pygame.draw.circle(pr,(*pid_col,80),(16,16),15,2)
            screen.blit(pr,(int(rp_screen.x-16),int(rp_screen.y-8)))
            # Name tag
            nt = F_SM.render(rp["name"],True,pid_col)
            screen.blit(nt,(int(rp_screen.x)-nt.get_width()//2, int(rp_screen.y)-32))
            # HP bar
            if rp["mhp"]>0:
                ratio = max(0,rp["hp"]/rp["mhp"])
                pygame.draw.rect(screen,C["black"],(int(rp_screen.x-20),int(rp_screen.y-22),40,5))
                hc=C["red"] if ratio<.3 else (C["orange"] if ratio<.6 else C["green"])
                pygame.draw.rect(screen,hc,(int(rp_screen.x-20),int(rp_screen.y-22),int(40*ratio),5))

        # Walls (culled, themed)
        wf=WALL_FRONTS[min(theme,len(WALL_FRONTS)-1)]; wt=WALL_TOPS[min(theme,len(WALL_TOPS)-1)]
        cx0=max(0,int((cam.pos.x-W//2)//TILE)-1); cx1=min(len(grid[0]),int((cam.pos.x+W//2)//TILE)+2)
        cy0=max(0,int((cam.pos.y-H//2)//TILE)-1); cy1=min(len(grid),int((cam.pos.y+H//2)//TILE)+2)
        for r3 in range(cy0,cy1):
            for c3 in range(cx0,cx1):
                if grid[r3][c3]=="wall":
                    wp=cam.apply(Vector2(c3*TILE,r3*TILE)); screen.blit(wf,(wp.x,wp.y)); screen.blit(wt,(wp.x,wp.y-14))

        # Lighting
        lmap.fill((20,12,30) if theme<2 else (40,12,8))
        pp2=cam.apply(player.pos)*LS
        lmap.blit(lp_s,(pp2.x-230*LS,pp2.y-230*LS),special_flags=pygame.BLEND_RGB_ADD)
        for t2 in torches:
            tp2=cam.apply(t2)*LS
            if not(0<tp2.x<lw and 0<tp2.y<lh): continue
            f=.88+math.sin(torch_t*7.3+t2.x*.01)*.12; fw=int(lt_s.get_width()*f); fh=int(lt_s.get_height()*f)
            ft=pygame.transform.scale(lt_s,(fw,fh)); lmap.blit(ft,(tp2.x-fw//2,tp2.y-fh//2),special_flags=pygame.BLEND_RGB_ADD)
            if theme==2:  # lava glow on floor
                lmap.blit(ll_s,(tp2.x-200*LS,tp2.y-200*LS),special_flags=pygame.BLEND_RGB_ADD)
        for p2 in projs:
            pps=cam.apply(p2.pos)*LS; lmap.blit(lj_s,(pps.x-100*LS,pps.y-100*LS),special_flags=pygame.BLEND_RGB_ADD)
        for e2 in enemies:
            if e2.is_boss():
                ep2=cam.apply(e2.pos)*LS; lmap.blit(lb_s,(ep2.x-320*LS,ep2.y-320*LS),special_flags=pygame.BLEND_RGB_ADD)
        screen.blit(pygame.transform.scale(lmap,(W,H)),(0,0),special_flags=pygame.BLEND_RGBA_MULT)

        # Color grade + FX
        screen.blit(COLOR_GRADE,(0,0)); room_clear_fx.draw(screen)
        if player.hit_t>0:
            fl=pygame.Surface((W,H)); fl.fill((160,0,0)); fl.set_alpha(int(player.hit_t/.15*115)); screen.blit(fl,(0,0))
        hp_r=max(0,player.hp/player.mhp); VIGNETTE.set_alpha(int(120+(1-hp_r)**2*120)); screen.blit(VIGNETTE,(0,0))

        # ── POLISHED HUD ──────────────────────────────────────────────────────
        ui_hp+=(player.hp-ui_hp)*.12; ui_xp+=(player.xp-ui_xp)*.12

        # Left panel background
        _draw_hud_panel(screen,10,10,350,230,210)

        # HP bar with icon + shine animation
        _draw_icon(screen,16,18,"hp",C["red"],16)
        hpr=max(0,ui_hp/player.mhp)
        hcol=C["red"] if hpr<.3 else (C["orange"] if hpr<.6 else C["green"])
        _draw_bar(screen,36,18,300,22,hpr,hcol,animated_shine=(hpr>.5),t=hud_t)
        screen.blit(F_MED.render(f"{int(player.hp)}/{player.mhp}",True,C["white"]),(148,19))

        # XP bar with icon
        _draw_icon(screen,16,48,"xp",C["cyan"],16)
        _draw_bar(screen,36,50,240,12,min(1,ui_xp/player.xp_next),C["cyan"],bg_col=(8,18,28))
        screen.blit(F_SM.render(f"LVL {player.level}",True,C["cyan"]),(284,48))

        # Weapon info
        wep=player.weapon; rc3=RARITY_COL[wep["rare"]]
        _draw_icon(screen,16,72,"sword",rc3,16)
        screen.blit(F_MED.render(player.wkey,True,rc3),(36,70))
        key2=player.wkey
        if wep["ammo"]>0:
            ammo_cur=player.ammo.get(key2,0); ammo_max=MAX_AMMO.get(key2,1)
            _draw_icon(screen,16,95,"ammo",C["white"] if ammo_cur>0 else C["red"],14)
            _draw_bar(screen,36,97,160,10,ammo_cur/ammo_max,
                      C["white"] if ammo_cur>ammo_max*.3 else C["red"])
            screen.blit(F_SM.render(f"{ammo_cur}/{ammo_max}",True,C["gray"]),(204,95))
        else:
            screen.blit(F_SM.render("MELEE",True,C["teal"]),(36,96))
        screen.blit(F_SM.render(f"DMG {player.stats()['dmg']}  CRIT {int(player.crit*100)}%  G:{player.coins}",True,C["gray"]),(16,114))

        # Skill cooldown bars with icons
        def skill_bar(x,y,name,icon_kind,cd,max_cd,col3,key_hint):
            ready=cd<=0
            bg=(30,25,45) if not ready else (int(col3[0]*.3),int(col3[1]*.3),int(col3[2]*.3))
            pygame.draw.rect(screen,bg,(x,y,94,30),border_radius=6)
            if not ready:
                _draw_bar(screen,x,y,94,30,1-cd/max_cd,(*col3,60),bg_col=(0,0,0,0),border=False)
            border_col=col3 if ready else (40,35,55)
            pygame.draw.rect(screen,border_col,(x,y,94,30),1,border_radius=6)
            _draw_icon(screen,x+4,y+7,icon_kind,col3,16)
            if ready:
                screen.blit(F_SM.render(f"[{key_hint}] RDY",True,C["black"] if ready else C["white"]),(x+22,y+9))
            else:
                screen.blit(F_SM.render(f"[{key_hint}]{cd:.1f}s",True,C["white"]),(x+22,y+9))

        # Skill cooldown bars with icons (class-specific labels)
        q_names={"Warrior":"WHIRL","Mage":"NOVA","Rogue":"SHADOW"}
        e_names={"Warrior":"IRON","Mage":"BLINK","Rogue":"SMOKE"}
        r_names={"Warrior":"CRY","Mage":"METEOR","Rogue":"KNIVES"}
        cn=player.class_name
        skill_bar(16,134,q_names.get(cn,"SPIN"),"sword",player.spin_cd,4.0,CLASSES[cn]["col"],"Q")
        skill_bar(118,134,e_names.get(cn,"SHLD"),"shield",player.shield_cd,10.0,CLASSES[cn]["col"],"E")
        skill_bar(220,134,r_names.get(cn,"TURR"),"coin",player.turret_cd,12.0,CLASSES[cn]["col"],"R")

        # Dash bar with icon
        _draw_icon(screen,16,173,"dash",C["cyan"] if player.dash_cd<=0 else C["gray"],16)
        if player.dash_cd<=0:
            if player.charge_t>0:
                cr=player.charge_t/player.MAX_CHARGE
                col_dc=C["gold"] if cr>.7 else C["cyan"]
                _draw_bar(screen,36,174,160,18,cr,col_dc,animated_shine=True,t=hud_t)
                screen.blit(F_SM.render("CHARGING",True,C["black"]),(90,176))
            else:
                pygame.draw.rect(screen,(20,60,30),(36,174,160,18),border_radius=5)
                pygame.draw.rect(screen,C["green"],(36,174,160,18),1,border_radius=5)
                screen.blit(F_SM.render("DASH READY [SPACE]",True,C["green"]),(40,176))
        else:
            _draw_bar(screen,36,174,160,18,1-player.dash_cd/.85,C["gray"])
            screen.blit(F_SM.render(f"DASH  {player.dash_cd:.1f}s",True,C["white"]),(40,176))

        # Class badge
        cd_cls = CLASSES.get(player.class_name,{})
        cls_col = cd_cls.get("col", C["white"])
        pygame.draw.rect(screen,(20,15,30),(16,200,140,18),border_radius=5)
        pygame.draw.rect(screen,cls_col,(16,200,140,18),1,border_radius=5)
        screen.blit(F_SM.render(f"◆ {player.class_name}",True,cls_col),(20,202))

        # Theme indicator
        theme_names=["Stone Depths","Crystal Caverns","Lava Realm"]
        tc=[(180,180,180),(100,200,255),(255,100,30)][theme]
        screen.blit(F_SM.render(f"  {theme_names[theme]}",True,tc),(90,202))

        # Right panel
        _draw_hud_panel(screen,W-210,10,200,140,210)
        screen.blit(F_BIG.render(f"DEPTH {floor}",True,C["purple"]),(W-205,15))
        screen.blit(F_MED.render(f"Souls  {kills}",True,C["orange"]),(W-205,50))
        screen.blit(F_MED.render(f"Score  {score}",True,C["gold"]),(W-205,74))
        screen.blit(F_SM.render(f"Best: {META.get('best_score',0)}",True,C["gray"]),(W-205,100))
        if combo.best>=2:
            screen.blit(F_SM.render(f"Combo: x{combo.best}",True,C["cyan"]),(W-205,118))

        # Weapon list panel
        _draw_hud_panel(screen,W-210,158,200,9*24+10,180)
        wlist=list(WEAPONS.keys())
        for i,wk in enumerate(wlist[:9]):
            yy=162+i*24; active=(wk==player.wkey); wd3=WEAPONS[wk]; rc4=RARITY_COL[wd3["rare"]]
            if active:
                pygame.draw.rect(screen,(40,30,60),(W-208,yy-1,196,22),border_radius=4)
                pygame.draw.rect(screen,rc4,(W-208,yy-1,196,22),1,border_radius=4)
            col4=rc4 if active else C["gray"]
            ammo3=player.ammo.get(wk,MAX_AMMO.get(wk,0)) if wd3["ammo"]>0 else -1
            empty=ammo3==0 and wd3["ammo"]>0
            label=f"{i+1}. {wk[:13]}"+("!" if empty else "")
            screen.blit(F_SM.render(label,True,col4),(W-204,yy+4))
        combo.draw(screen); draw_relic_hud(screen,player)
        minimap.draw(screen,player,enemies,stairs,torches,shop_pos,shop_used)

        # ── MULTIPLAYER UI ──
        if len(all_peers) > 1:
            # Player list (bottom left above HUD)
            panel_y = H - 44 - len(all_peers)*32 - 10
            _draw_hud_panel(screen, 10, panel_y, 220, len(all_peers)*32+8, 180)
            for i, p in enumerate(all_peers):
                pid2  = p["pid"]; col_p2 = COLORS_MP[pid2%len(COLORS_MP)]
                is_me = (pid2==my_pid)
                py2   = panel_y+4+i*32
                # Dot
                pygame.draw.circle(screen,col_p2,(24,py2+12),6)
                name2 = p["name"]+(" (you)" if is_me else "")
                screen.blit(F_SM.render(name2,True,col_p2),(34,py2+4))
                # HP mini bar
                if is_me:
                    hp_r2 = max(0,player.hp/player.mhp)
                else:
                    rp2 = remote_players.get(pid2)
                    hp_r2 = max(0,rp2["hp"]/max(1,rp2["mhp"])) if rp2 else 0
                pygame.draw.rect(screen,(30,25,40),(140,py2+10,70,8),border_radius=3)
                hc2=C["red"] if hp_r2<.3 else (C["orange"] if hp_r2<.6 else C["green"])
                if int(70*hp_r2)>0:
                    pygame.draw.rect(screen,hc2,(140,py2+10,int(70*hp_r2),8),border_radius=3)

        # Chat messages (bottom center)
        chat_y_start = H - 180
        for i,(msg,_) in enumerate(chat_msgs[-5:]):
            ct = F_SM.render(msg,True,C["white"])
            cs = pygame.Surface((ct.get_width()+8,ct.get_height()+4),pygame.SRCALPHA)
            cs.fill((0,0,0,120))
            cs.blit(ct,(4,2))
            screen.blit(cs,(W//2-cs.get_width()//2, chat_y_start+i*22))

        # Chat input box
        if chat_open:
            ci_rect = pygame.Rect(W//2-250,H-55,500,38)
            pygame.draw.rect(screen,(20,16,30),ci_rect,border_radius=6)
            pygame.draw.rect(screen,C["cyan"],ci_rect,2,border_radius=6)
            screen.blit(F_SM.render("[T] Chat: "+chat_input+"_",True,C["cyan"]),(W//2-242,H-44))
        elif len(all_peers)>1:
            screen.blit(F_SM.render("[T] Chat",True,(60,60,80)),(W//2-25,H-20))

        # Revive
        if player.hp<=0 and ("phoenix" in player.relics or "revive" in [u for u,v in META.get("upgrades",{}).items() if v>0]) and not player.revive_used:
            player.hp=player.mhp; player.revive_used=True
            ps.emit(player.pos.x,player.pos.y,40,C["gold"],250,.6,5); ps.emit_txt(player.pos.x,player.pos.y-40,"REVIVED!",C["gold"],True); cam.hit(10,.4)

        # Game over
        if player.hp<=0:
            screen.fill((60,0,0,220),special_flags=pygame.BLEND_RGBA_ADD)
            for txt,col5,y in[("YOU DIED",C["red"],H//2-90),(f"Depth:{floor}  Level:{player.level}  Score:{score}",C["white"],H//2-10),(f"Best Combo: x{combo.best}  |  Kills: {kills}",C["cyan"],H//2+20)]:
                f5=F_HUGE if "DIED" in txt else F_BIG; t5=f5.render(txt,True,col5); screen.blit(t5,(W//2-t5.get_width()//2,y))
            pygame.display.flip(); pygame.time.wait(600)
            if net_host: net_host.stop()
            if net_client: net_client.stop()
            res=meta_screen(screen,META,score,floor)
            if res=="restart": return main()

        pygame.display.flip()

if __name__=="__main__":
    main()