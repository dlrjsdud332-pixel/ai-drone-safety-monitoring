"""Build staged industrial scenes while copying the existing X500 flight setup."""
from copy import deepcopy
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zlib

HERE = Path(__file__).resolve().parent
MODELS = HERE/'models'
TEXTURES = MODELS/'industrial_materials/materials/textures'


def child(parent, tag, text=None, **attributes):
    node = ET.SubElement(parent, tag, attributes)
    if text is not None:
        node.text = str(text)
    return node


def save(tree, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space='  ')
    tree.write(path, encoding='utf-8', xml_declaration=True)


def material(node, color, metal=0, rough=0.8):
    m = child(node, 'material')
    child(m, 'ambient', color)
    child(m, 'diffuse', color)
    p = child(child(m, 'pbr'), 'metal')
    child(p, 'metalness', metal)
    child(p, 'roughness', rough)
    return m, p


def box(link, name, pose, size, color, collision=False, metal=0, rough=0.8):
    visual = child(link, 'visual', name=name)
    child(visual, 'pose', pose)
    child(child(child(visual, 'geometry'), 'box'), 'size', size)
    material(visual, color, metal, rough)
    if collision:
        c = child(link, 'collision', name=name+'_collision')
        child(c, 'pose', pose)
        child(child(child(c, 'geometry'), 'box'), 'size', size)
    return visual


def model(world, name, pose):
    m = child(world, 'model', name=name)
    child(m, 'static', 'true')
    child(m, 'pose', pose)
    return child(m, 'link', name='structure')


def building(world, name, x, y, length, width, height):
    link = model(world, name, f'{x} {y} 0 0 0 0')
    wall = '0.38 0.43 0.46 1'
    trim = '0.14 0.18 0.2 1'
    box(link, 'foundation', '0 0 0.15 0 0 0', f'{length+0.6} {width+0.6} 0.3', '0.47 0.46 0.43 1', True)
    box(link, 'shell', f'0 0 {height/2} 0 0 0', f'{length} {width} {height}', wall, True, 0.35)
    # Shallow pitched roof, flashing, standing seams and loading-door detail.
    roof_angle = 0.10
    for sign in [-1, 1]:
        box(link, f'roof_{sign}', f'0 {sign*width/4} {height+0.15} {-sign*roof_angle} 0 0',
            f'{length+0.7} {width/2+0.5} 0.12', trim, metal=0.65, rough=0.55)
    for i in range(int(length/0.65)+1):
        xpos = -length/2+i*0.65
        box(link, f'cladding_{i}', f'{xpos} {-width/2-0.035} {height/2} 0 0 0',
            f'0.045 0.08 {height}', '0.46 0.50 0.52 1', metal=0.45)
    for index, xpos in enumerate([-length/4, length/4]):
        box(link, f'loading_frame_{index}', f'{xpos} {-width/2-0.09} 1.85 0 0 0',
            '3.4 0.12 3.7', trim, metal=0.5)
        box(link, f'loading_door_{index}', f'{xpos} {-width/2-0.17} 1.75 0 0 0',
            '3.1 0.08 3.4', '0.56 0.59 0.60 1', metal=0.5)
        for row in range(12):
            box(link, f'door_seam_{index}_{row}', f'{xpos} {-width/2-0.22} {0.15+row*0.29} 0 0 0',
                '3.05 0.02 0.018', trim)
        box(link, f'window_{index}', f'{xpos} {-width/2-0.23} {height-0.9} 0 0 0',
            '2.7 0.025 0.55', '0.12 0.23 0.29 1', metal=0.25, rough=0.23)
    for sign in [-1, 1]:
        box(link, f'column_{sign}', f'{sign*(length/2-0.1)} {-width/2-0.13} {height/2} 0 0 0',
            f'0.18 0.2 {height}', trim, metal=0.6)


def mesh_visual(link, name, uri):
    visual = child(link, 'visual', name=name)
    child(child(child(visual, 'geometry'), 'mesh'), 'uri', uri)
    return visual


def create_meshes():
    folder = MODELS/'industrial_materials/meshes'
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'ground.obj').write_text(
        '# Original CC0 geometry. 3m texture repeat on a 60m slab.\n'
        'v -30 -30 0\nv 30 -30 0\nv 30 30 0\nv -30 30 0\n'
        'vt 0 0\nvt 20 0\nvt 20 20\nvt 0 20\nvn 0 0 1\n'
        'f 1/1/1 2/2/1 3/3/1\nf 1/1/1 3/3/1 4/4/1\n')
    # Single mesh per panel, avoiding a separate scene node for each wire.
    vertices, faces = [], []
    def rod(x, z, sx, sz):
        base = len(vertices)
        for dx, dy, dz in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                           (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]:
            vertices.append((x+dx*sx/2, dy*0.009, z+dz*sz/2))
        for a,b,c,d in [(1,4,3,2),(5,6,7,8),(1,2,6,5),(4,8,7,3),(1,5,8,4),(2,3,7,6)]:
            faces.extend([(base+a,base+b,base+c),(base+a,base+c,base+d)])
    for i in range(26):
        rod(-1.5+i*0.12, 1.1, 0.012, 2.2)
    for j in range(19):
        rod(0, j*0.12, 3, 0.012)
    for x in [-1.5,1.5]:
        rod(x, 1.1, 0.065, 2.25)
    for z in [0.08,2.17]:
        rod(0,z,3.05,0.055)
    (folder/'fence.obj').write_text('# Original CC0 steel fence mesh.\n'+
        ''.join('v %.5f %.5f %.5f\n'%v for v in vertices)+
        ''.join('f %d %d %d\n'%f for f in faces))


def png(path, width, height, rgba):
    def chunk(kind, payload):
        return struct.pack('!I',len(payload))+kind+payload+struct.pack('!I',zlib.crc32(kind+payload)&0xffffffff)
    rows = b''.join(b'\x00'+rgba[y*width*4:(y+1)*width*4] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,6,0,0,0))+
                     chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


def particle_texture(name, flame=False):
    pixels = bytearray()
    for y in range(64):
        for x in range(64):
            u,v=(x-31.5)/31.5,(y-31.5)/31.5
            radius=(u*(1.3 if flame else 1))**2+v*v
            alpha=int(255*max(0,1-radius)**2)
            pixels.extend((255,255,255,alpha))
    png(TEXTURES/name,64,64,bytes(pixels))


def add_effects(world):
    child(world,'plugin',filename='gz-sim-particle-emitter-system',name='gz::sim::systems::ParticleEmitter')
    link = model(world, 'industrial_fire', '13 3 0 0 -1.57079632679 0')
    for name, rate, life, color_start, color_end, size, speed, growth in [
        ('smoke',12,8,'0.28 0.29 0.30 0.65','0.48 0.49 0.50 0',0.55,0.65,0.16),
        ('flame',24,1.5,'1 0.8 0.18 0.95','0.8 0.12 0.01 0',0.3,0.75,0.1)]:
        e=child(link,'particle_emitter',name=name,type='box')
        child(e,'pose','-0.3 0 0 0 0 0')
        for key,value in [('emitting','true'),('size','0.35 0.45 0.45'),('particle_size',f'{size} {size} {size}'),
                          ('rate',rate),('lifetime',life),('min_velocity',speed),('max_velocity',speed+0.2),
                          ('scale_rate',growth),('color_start',color_start),('color_end',color_end)]:
            child(e,key,value)
        m,p=material(e,'1 1 1 1',rough=1)
        child(p,'albedo_map',f'model://industrial_materials/materials/textures/{name}.png')
    # A small emissive core, not an extra dynamic point light or rigid fake flame.
    core=model(world,'fire_core','13 3 0 0 0 0')
    v=box(core,'charred_pallet','0 0 0.12 0 0 0','1.2 0.9 0.24','0.12 0.08 0.04 1',True)
    visual=box(core,'embers','0 0 0.27 0 0 0','0.75 0.65 0.08','0.6 0.10 0.01 1')
    child(visual.find('material'),'emissive','1 0.22 0.015 1')


def main():
    TEXTURES.mkdir(parents=True,exist_ok=True)
    create_meshes()
    particle_texture('smoke.png')
    particle_texture('flame.png',True)
    baseline=ET.parse(HERE.parent/'x500_camera.sdf')
    world=baseline.getroot().find('world')
    for m in list(world.findall('model')):
        if m.get('name','').startswith('safety_'):
            world.remove(m)
    include=child(world,'include')
    child(include,'uri','model://industrial_worker')
    child(include,'name','industrial_worker_01')
    child(include,'pose','8 0 0 0 0 0')
    save(baseline,HERE/'worker_only.sdf')
    site=deepcopy(baseline)
    world=site.getroot().find('world')
    ground=world.find("model[@name='ground_plane']/link/visual")
    ground.remove(ground.find('geometry'))
    ground.remove(ground.find('material'))
    child(child(child(ground,'geometry'),'mesh'),'uri','model://industrial_materials/meshes/ground.obj')
    m,p=material(ground,'1 1 1 1')
    for tag,name in [('albedo_map','concrete_diff.jpg'),('normal_map','concrete_normal.jpg'),('roughness_map','concrete_rough.jpg')]:
        child(p,tag,f'model://industrial_materials/materials/textures/{name}')
    building(world,'factory',17,12,22,9,6)
    building(world,'warehouse',17,-13,18,8,5)
    for y in [-6,6]:
        for x in [6,9,12,15,18,21]:
            l=model(world,f'fence_{x}_{y}',f'{x} {y} 0 0 0 0')
            v=mesh_visual(l,'steel_panel','model://industrial_materials/meshes/fence.obj')
            material(v,'0.36 0.39 0.38 1',metal=0.75,rough=0.5)
            c=child(l,'collision',name='panel_collision')
            child(c,'pose','0 0 1.1 0 0 0')
            child(child(child(c,'geometry'),'box'),'size','3 0.06 2.2')
    markings=model(world,'yard_markings','0 0 0 0 0 0')
    for y in [-4.8,4.8]:
        box(markings,f'line_{y}','14 '+str(y)+' 0.003 0 0 0','24 0.12 0.004','0.84 0.68 0.12 1')
    for index in range(4):
        l=model(world,f'loading_pallet_{index}',f'{18+index*1.4} -4 0 0 0 0')
        box(l,'load','0 0 0.65 0 0 0','1.1 0.85 1.2','0.55 0.46 0.30 1',True)
        for z in [0.2,0.7,1.2]:
            box(l,f'strap_{z}',f'0 0 {z} 0 0 0','1.13 0.88 0.035','0.18 0.21 0.20 1')
    save(site,HERE/'industrial_site.sdf')
    effects=deepcopy(site)
    add_effects(effects.getroot().find('world'))
    save(effects,HERE/'industrial_effects.sdf')
    print('Built worker_only, industrial_site, industrial_effects. Asset installation and rendering are separate checks.')


if __name__=='__main__':
    main()
