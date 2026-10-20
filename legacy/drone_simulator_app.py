#!/usr/bin/env python3
"""Local, simulation-only multi-drone cockpit and UDP bridge for drone_tracker.py.
No hardware, MAVLink or live aircraft commands are sent.
"""
import json
import math
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

HOST = '127.0.0.1'
TELEMETRY_PORT = 14560
COMMAND_PORT = 14561
TICK_MS = 70


@dataclass
class Drone:
    identifier: str
    x: float
    y: float
    home_x: float = 0.0
    home_y: float = 0.0
    altitude: float = 0.0
    battery: float = 100.0
    heading: float = 0.0
    mode: str = 'IDLE'
    armed: bool = False
    route: list = field(default_factory=list)
    waypoint: int = 0
    mission_total: int = 0
    command: str = '-'
    last_id: str = ''
    def __post_init__(self):
        self.home_x, self.home_y = self.x, self.y


def move_toward(d, tx, ty, speed, dt):
    dx, dy = tx - d.x, ty - d.y
    distance = math.hypot(dx, dy)
    if distance < 0.5:
        d.x, d.y = tx, ty
        return True
    step = min(distance, speed * dt)
    d.x += dx / distance * step
    d.y += dy / distance * step
    d.heading = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
    return step >= distance


def simulate_step(d, dt):
    if d.mode == 'TAKEOFF':
        d.armed = True
        d.altitude = min(10.0, d.altitude + 2.5 * dt)
        if d.altitude >= 10:
            d.mode = 'MISSION'
    elif d.mode == 'MISSION':
        if d.waypoint < len(d.route):
            x, y = d.route[d.waypoint]
            if move_toward(d, x, y, 20.0, dt):
                d.waypoint += 1
        else:
            d.mode = 'RTL'
    elif d.mode == 'RTL':
        d.armed = True
        if move_toward(d, d.home_x, d.home_y, 25.0, dt):
            d.mode = 'LAND'
    elif d.mode == 'LAND':
        d.altitude = max(0., d.altitude - 2.5 * dt)
        if d.altitude <= 0:
            d.armed = False
            d.mode = 'LANDED'
    if d.armed:
        d.battery = max(0.0, d.battery - (0.06 + (0.02 if d.mode == 'MISSION' else 0)) * dt)
        if d.battery <= 12 and d.mode not in ('RTL', 'LAND', 'LANDED'):
            d.mode = 'RTL'


def telemetry_packet(d):
    """A packet compatible with TelemetryInputAdapter.ingest(). GPS intentionally absent."""
    return {
        'drone_id': d.identifier,
        'battery': round(d.battery, 1),
        'altitude_m': round(d.altitude, 2),
        'heading_deg': round(d.heading, 1),
        'flight_mode': d.mode,
        'armed': d.armed,
        'last_command': d.command,
        'last_command_id': d.last_id or None,
        'simulation': True,
    }


class LocalBridge:
    def __init__(self, host=HOST, telemetry_port=TELEMETRY_PORT, command_port=COMMAND_PORT):
        self.host, self.telemetry_port, self.command_port = host, telemetry_port, command_port
        self.running = False
        self.received = queue.Queue()
        self.status = 'OFFLINE'
        self.listen_sock = None
        self.send_sock = None
        self.worker = None

    def start(self):
        if self.running:
            return True
        try:
            self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.listen_sock.bind((self.host, self.command_port))
            self.listen_sock.settimeout(0.2)
            self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.running = True
            self.status = 'UDP ON'
            self.worker = threading.Thread(target=self._listen, daemon=True)
            self.worker.start()
            return True
        except OSError as exc:
            self.status = f'PORT ERROR: {exc}'
            self.close()
            return False

    def _listen(self):
        while self.running:
            try:
                data, address = self.listen_sock.recvfrom(8192)
                if address[0] not in ('127.0.0.1', '::1'):
                    continue
                obj = json.loads(data.decode('utf-8'))
                if isinstance(obj, dict):
                    self.received.put(obj)
            except socket.timeout:
                continue
            except (OSError, ValueError, UnicodeDecodeError):
                if not self.running:
                    break

    def transmit(self, packet):
        if not self.running or not self.send_sock:
            return
        try:
            self.send_sock.sendto(json.dumps(packet).encode('utf-8'), (self.host, self.telemetry_port))
        except OSError as exc:
            self.status = f'SEND ERROR: {exc}'

    def close(self):
        self.running = False
        for sock in (self.listen_sock, self.send_sock):
            if sock is not None:
                try: sock.close()
                except OSError: pass
        self.listen_sock, self.send_sock = None, None
        if self.status == 'UDP ON':
            self.status = 'OFFLINE'


class SimulatorApp:
    def __init__(self, root):
        self.root = root
        self.root.title('Drone Lab | Multi-Drone Simulator — LOCAL ONLY')
        self.root.geometry('1120x760')
        self.root.minsize(900, 620)
        self.root.configure(bg='#101722')
        self.bridge = LocalBridge()
        self.drones = [Drone('SIM-01', 115, 255), Drone('SIM-02', 240, 255), Drone('SIM-03', 365, 255)]
        self.selected = tk.StringVar(value='SIM-01')
        self.running_sim = True
        self.battery_var = tk.DoubleVar(value=100)
        self.status = tk.StringVar(value='LOCAL ONLY · No real aircraft connected')
        self.detail = tk.StringVar(value='')
        self.last_time = time.monotonic()
        self.last_udp = 0.
        self._build()
        self.root.protocol('WM_DELETE_WINDOW', self.quit)
        self._tick()

    def _build(self):
        title = tk.Frame(self.root, bg='#101722')
        title.pack(fill='x', padx=20, pady=(15, 5))
        tk.Label(title, text='DRONE LAB', fg='#e7f0ff', bg='#101722', font=('Helvetica', 21, 'bold')).pack(side='left')
        tk.Label(title, text='  Multi-Drone Simulator  |  2D / UDP / Safety Tests', fg='#9baec6', bg='#101722', font=('Helvetica', 12)).pack(side='left', pady=7)
        main = tk.Frame(self.root, bg='#101722')
        main.pack(fill='both', expand=True, padx=20, pady=8)
        self.canvas = tk.Canvas(main, bg='#152436', highlightthickness=1, highlightbackground='#3b5065')
        self.canvas.pack(side='left', fill='both', expand=True)
        side = tk.Frame(main, bg='#1d2938', width=295)
        side.pack(side='right', fill='y', padx=(10, 0))
        side.pack_propagate(False)
        def label(text, size=11, color='#dae5f2'):
            tk.Label(side, text=text, font=('Helvetica', size, 'bold'), bg='#1d2938', fg=color, anchor='w').pack(fill='x', padx=14, pady=(13, 4))
        def btn(text, command, color='#2c4359'):
            tk.Button(side, text=text, command=command, bg=color, fg='white', activebackground='#425c73', activeforeground='white', relief='flat', height=2, font=('Helvetica', 11, 'bold')).pack(fill='x', padx=14, pady=4)
        label('SELECT DRONE', 12)
        for d in self.drones:
            tk.Radiobutton(side, text=d.identifier, variable=self.selected, value=d.identifier, command=self._selection_change, bg='#1d2938', fg='#e7f0ff', selectcolor='#34536b', activebackground='#1d2938', activeforeground='white', font=('Helvetica', 11)).pack(anchor='w', padx=14)
        label('FLIGHT COMMANDS', 12)
        btn('▶ TAKEOFF + WAYPOINT MISSION', self.start_mission, '#256d72')
        btn('↩ RETURN TO HOME (SIM RTL)', self.rtl, '#98652c')
        btn('■ LAND HERE', self.land, '#713f4f')
        btn('⟳ RESET SELECTED', self.reset)
        label('BATTERY TEST', 12)
        ttk.Scale(side, from_=1, to=100, orient='horizontal', variable=self.battery_var, command=self._battery_change).pack(fill='x', padx=14, pady=(3, 6))
        label('INTEGRATION', 12)
        btn('Connect UDP Bridge', self.toggle_bridge, '#32609b')
        tk.Label(side, text='RX commands 127.0.0.1:14561\nTX telemetry 127.0.0.1:14560\nOnly SIM-01 sent to legacy tracker', bg='#1d2938', fg='#aabdd1', justify='left', font=('Helvetica', 10)).pack(fill='x', padx=14, pady=5)
        label('LIVE STATE', 12)
        tk.Label(side, textvariable=self.detail, bg='#1d2938', fg='#d6f0e3', justify='left', font=('Menlo', 10), anchor='nw').pack(fill='both', padx=14, pady=(3, 8))
        tk.Label(self.root, textvariable=self.status, bg='#101722', fg='#9ad2d1', font=('Menlo', 10), anchor='w').pack(fill='x', padx=20, pady=(0, 10))

    def current(self):
        return next(d for d in self.drones if d.identifier == self.selected.get())

    def _selection_change(self):
        self.battery_var.set(self.current().battery)

    def _battery_change(self, _=None):
        self.current().battery = float(self.battery_var.get())

    def start_mission(self):
        d = self.current()
        if d.battery <= 15:
            self.status.set(f'{d.identifier}: LOW BATTERY — mission blocked')
            return
        d.route = [(d.home_x+50, d.home_y-115), (d.home_x+170, d.home_y-110), (d.home_x+200, d.home_y+5)]
        d.mission_total = len(d.route)
        d.waypoint = 0
        d.mode = 'TAKEOFF'
        d.armed = True
        d.command = 'START_MISSION'
        self.status.set(f'{d.identifier}: SIM mission started')

    def rtl(self):
        d = self.current()
        if d.armed:
            d.mode = 'RTL'
            d.command = 'RTH'
        else:
            self.status.set(f'{d.identifier}: not airborne')

    def land(self):
        d = self.current()
        if d.armed:
            d.mode = 'LAND'
            d.command = 'LAND'

    def reset(self):
        d = self.current()
        d.x, d.y = d.home_x, d.home_y
        d.altitude, d.heading, d.battery = 0., 0., 100.
        d.mode, d.armed, d.route, d.waypoint, d.mission_total = 'IDLE', False, [], 0, 0
        d.command, d.last_id = '-', ''
        self.battery_var.set(d.battery)
        self.status.set(f'{d.identifier}: reset')

    def toggle_bridge(self):
        if self.bridge.running:
            self.bridge.close()
            self.status.set('UDP disconnected')
        elif self.bridge.start():
            self.status.set('UDP connected — SIM-01 telemetry → drone_tracker.py')
        else:
            self.status.set(self.bridge.status)

    def _consume_commands(self):
        # Existing tracker has one shared telemetry store. Only SIM-01 is mapped.
        d = self.drones[0]
        for _ in range(20):
            try: obj = self.bridge.received.get_nowait()
            except queue.Empty: break
            command = str(obj.get('command', '')).upper()
            command_id = str(obj.get('command_id', ''))
            if not obj.get('safety', {}).get('approved', True):
                self.status.set('BLOCKED incoming unapproved command')
                continue
            d.command, d.last_id = command, command_id
            # Agent's PATROL/TRACK/ORBIT are not exact safe motion instructions.
            # DO NOT automatically fly for these abstract commands.
            if command == 'RTH':
                if d.armed: d.mode = 'RTL'
            elif command == 'LAND':
                if d.armed: d.mode = 'LAND'
            elif command in ('PATROL', 'TRACK', 'ORBIT'):
                self.status.set(f'{command}: logged; no movement without operator approval')
            else:
                self.status.set(f'UDP received: {command} (simulation)')

    def _draw(self):
        c = self.canvas
        c.delete('all')
        w, h = max(c.winfo_width(), 200), max(c.winfo_height(), 200)
        sx, sy = w/620., h/560.
        def coord(x, y): return 50+x*sx*.85, 35+y*sy*.82
        for i in range(0, 900, 40):
            x1, y1 = coord(i, 0)
            x2, y2 = coord(i, 610)
            c.create_line(x1,y1,x2,y2,fill='#22394b')
        for j in range(0, 750, 40):
            x1,y1=coord(0,j); x2,y2=coord(850,j)
            c.create_line(x1,y1,x2,y2,fill='#22394b')
        c.create_text(20, 20, anchor='w', text='TRAINING AIRSPACE   ·   SIMULATION ONLY', fill='#a0d5e7',font=('Helvetica', 12, 'bold'))
        colors = ['#56d4c1','#e7af58','#b4a2f6']
        for i,d in enumerate(self.drones):
            hx,hy=coord(d.home_x,d.home_y)
            c.create_oval(hx-17,hy-17,hx+17,hy+17,outline=colors[i],dash=(3,4),width=2)
            c.create_text(hx,hy+28,text='HOME',fill='#6b889d',font=('Helvetica',9))
            pts=[(d.home_x,d.home_y)]+d.route
            if d.route:
                p=[]
                for x,y in pts: p.extend(coord(x,y))
                c.create_line(*p, fill=colors[i], width=2, dash=(5,5))
                for n,(x,y) in enumerate(d.route):
                    px,py=coord(x,y)
                    c.create_oval(px-10,py-10,px+10,py+10,fill='#263e50',outline=colors[i])
                    c.create_text(px,py,text=str(n+1),fill='#e7f0ff',font=('Helvetica',9))
            x,y=coord(d.x,d.y)
            radius = 17 if d.identifier == self.selected.get() else 13
            c.create_oval(x-radius,y-radius,x+radius,y+radius,fill=colors[i],outline='#e3eaf3',width=2)
            c.create_text(x,y,text='▲',fill='#172333',font=('Helvetica',12,'bold'))
            c.create_text(x,y-32,text=d.identifier,fill=colors[i],font=('Helvetica',11,'bold'))
            c.create_text(x,y+43,text=f'{d.mode}  {d.altitude:.0f}m  {d.battery:.0f}%',fill='#bfd0df',font=('Menlo',9))

    def _tick(self):
        now = time.monotonic()
        dt = min(max(now-self.last_time, 0.0), .2)
        self.last_time = now
        for d in self.drones: simulate_step(d,dt)
        self._consume_commands()
        if self.bridge.running and now-self.last_udp > .25:
            self.bridge.transmit(telemetry_packet(self.drones[0]))
            self.last_udp=now
        d = self.current()
        if not self.root.focus_get() or self.root.focus_get().winfo_class() != 'TScale':
            self.battery_var.set(d.battery)
        pct = round(100*d.waypoint/d.mission_total) if d.mission_total else 0
        self.detail.set(f'ID      {d.identifier}\nMODE    {d.mode}\nARMED   {d.armed}\nBATTERY {d.battery:5.1f}%\nALT     {d.altitude:5.1f} m\nHEADING {d.heading:5.0f}°\nWAYPT   {d.waypoint}/{d.mission_total}  ({pct}%)\nUDP     {self.bridge.status}')
        self._draw()
        self.root.after(TICK_MS,self._tick)

    def quit(self):
        self.bridge.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    SimulatorApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
