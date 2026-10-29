"""Real Transfer folder operations against the in-memory Data RPC device."""
import base64,copy,io,json,struct,sys,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'synfact-transfer'))
import core as C
import compaction as K
import app as A

def wav():
    out=io.BytesIO()
    with wave.open(out,'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(48000);f.writeframes(b'\0\0'*16)
    return base64.b64encode(out.getvalue()).decode()

class FolderTests(unittest.TestCase):
    def setUp(self):
        self.device=C.H.FakeHZ(0x680*1024);self.manager=C.Manager();self.manager.dev=self.device;self.manager.profile='v7'
        self.store=C.H.Store(self.device,C.S,log=lambda *a:None)
        C.H.cmd_init(self.store)
    def run_action(self,action,**args):return self.manager.run(action,args,lambda *a:None,lambda:False)
    def test_real_migration_moves_and_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_action('folder_enable',backup=str(Path(tmp)/'before'),confirm_firmware=True)
            d=self.run_action('folder_create',name='Drums')['id']
            child=self.run_action('folder_create',name='Acoustic',parent=d)['id']
            put=self.run_action('sample_put',data=wav(),name='Kick',folder=child,extended=True)
            self.assertEqual(put['source'],'high');idx,_=self.store.current();e=idx.find('1');before=dict(e)
            self.assertEqual(e['folder'],child)
            self.run_action('folder_change',id=d,name='Percussion')
            self.run_action('folder_move_samples',ids=[1],folder=0)
            idx,_=self.store.current();self.assertEqual(idx.find('1')['folder'],0)
            self.assertEqual({k:v for k,v in idx.find('1').items() if k!='folder'},{k:v for k,v in before.items() if k!='folder'})
            self.run_action('folder_remove',id=child);self.run_action('folder_remove',id=d)
            idx,_=self.store.current();self.assertEqual(idx.folders,[])
    def test_rejected_changes_do_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_action('folder_enable',backup=str(Path(tmp)/'before'),confirm_firmware=True)
            parent=self.run_action('folder_create',name='A')['id'];child=self.run_action('folder_create',name='B',parent=parent)['id']
            before=[self.device.window_bytes(k) for k in C.H.INDEX_K]
            for op,args in [('folder_change',dict(id=parent,parent=child)),('folder_remove',dict(id=parent)),('folder_create',dict(name='A'))]:
                with self.assertRaises(ValueError):self.run_action(op,**args)
                self.assertEqual([self.device.window_bytes(k) for k in C.H.INDEX_K],before)
    def test_compaction_preserves_hierarchy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_action('folder_enable',backup=str(Path(tmp)/'metadata'),confirm_firmware=True)
            d=self.run_action('folder_create',name='Pads')['id']
            self.run_action('sample_put',data=wav(),name='Pad',folder=d,extended=True)
            before,_=self.store.current()
            K.compact(self.store,lambda *a:None,lambda:False)
            after,_=self.store.current();self.assertEqual(after.version,2);self.assertEqual(after.folders,before.folders)
            self.assertEqual(after.find('1')['folder'],d)
    def test_explicit_migration_and_write_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            before=[self.device.window_bytes(k) for k in C.H.INDEX_K]
            with self.assertRaises(ValueError):self.run_action('folder_enable',backup=str(Path(tmp)/'before'))
            self.assertEqual([self.device.window_bytes(k) for k in C.H.INDEX_K],before)
        for op in ('folder_enable','folder_create','folder_change','folder_remove','folder_move_samples'):
            self.assertIn(op,A.WRITE_ACTIONS)

if __name__=='__main__':unittest.main()
