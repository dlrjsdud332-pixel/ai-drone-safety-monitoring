"""Run real editor JS against synthetic DOM/canvas stubs, not a browser test."""
import argparse
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('html',type=Path);args=parser.parse_args()
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtQml import QJSEngine
    app=QCoreApplication.instance() or QCoreApplication([]);engine=QJSEngine()
    snippets=[]
    def run(code):
        snippets.append(code)
        if len(snippets)<3:return
        result=engine.evaluate("\n".join(snippets))
        if result.isError():raise AssertionError(result.toString()+' line '+result.property('lineNumber').toString())
        return result
    run('''
const elements={};
function element(){return {value:'',checked:false,width:1920,height:1080,textContent:'',append(){},replaceChildren(){},click(){},setPointerCapture(){},getBoundingClientRect(){return {left:0,top:0,width:1920,height:1080};},getContext(){return {drawImage(){},strokeRect(){},fillText(){}};}};}
const document={getElementById(id){if(!elements[id])elements[id]=element();return elements[id];},createElement(){return element();}};
const window={addEventListener(){}};
function Image(){this.naturalWidth=1920;this.naturalHeight=1080;Object.defineProperty(this,'src',{set(v){this.onload();}});}
function FileReader(){this.readAsText=function(file){this.result=file.content;this.onload();};}
function alert(s){throw Error('Unexpected alert: '+s);}function confirm(){return true;}
function assert(value,message){if(!value)throw Error(message);}
''')
    html=args.html.read_text();script=html.split('<script>')[1].split('</script>')[0]
    run(script)
    run('''
assert(state.length===30,'30 frames');assert(state.every(s=>s.status==='pending'),'all unreviewed');
assert(DATA.records.slice(0,10).every(r=>r.priority_day_fire),'day fire priority');
assert(state[0].boxes.length>0,'automatic preload');
const saved=JSON.parse(JSON.stringify(state));
choose(0);$('x1').value=100;$('y1').value=100;$('x2').value=200;$('y2').value=200;
$('applyedit').onclick();assert(state[0].boxes[0][3]===100/1920,'coordinate edit');assert(state[0].status==='pending','edit invalidates');
$('reviewer').value='SYNTHETIC JS FIXTURE';$('firepresent').checked=state[0].boxes.some(b=>b[0]===1);$('complete').checked=true;
$('approve').onclick();assert(state[0].status==='completed','explicit completion');
choose(0);$('applyedit').onclick();assert(state[0].status==='pending','modified completion revoked');
const oldOrder=saved.slice().reverse().map(s=>{delete s.box_metadata;return s;});
$('restore').onchange({target:{files:[{content:JSON.stringify({dataset_fingerprint:DATA.dataset_fingerprint,records:oldOrder})}]}});
assert(state[0].id===DATA.records[0].id,'restore by ID after reorder');
assert(state[0].boxes[0][3]===saved[0].boxes[0][3],'saved boxes restored');
assert(state[0].box_metadata.length===state[0].boxes.length,'legacy JSON supported');
load(state.findIndex(s=>s.boxes.length===0));assert(state[index].boxes.length===0,'empty selectable');
''')
    print('PASS: JS preload, day-first order, coordinate edit, explicit completion, revocation, reordered/legacy JSON restore, empty-frame selection')
    print('Synthetic DOM/canvas only; real browser rendering/drag/download not verified.')


if __name__=='__main__':main()
