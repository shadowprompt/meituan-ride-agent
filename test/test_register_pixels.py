import copy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from register_pixels import fit_registration, transform, register


def spec():
    def point(x,y):
        return {'pixel_xy':[x,y], 'wgs84':[x*0.00001, -y*0.00001]}
    return {'coordinate_system':'WGS84','pixel_frame':'local_mask', 'synthetic':True,
            'control_points':[point(0,0),point(640,0),point(0,480),point(640,480)],
            'check_points':[point(100,100),point(500,300)]}


class RegistrationTests(unittest.TestCase):
    def test_exact_and_transform(self):
        model = fit_registration(spec())
        self.assertTrue(model['independent_error_gate_passed'])
        self.assertLess(model['fit_rmse_m'], 1e-8)
        np.testing.assert_allclose(transform([[200,100]],model), [[.002,-.001]], atol=1e-10)
        self.assertFalse(model['verified'])

    def test_no_independent_checks(self):
        s=spec();s['check_points']=[]
        m=fit_registration(s)
        self.assertFalse(m['independent_error_gate_passed'])
        self.assertIsNone(m['check_rmse_m'])

    def test_wrong_checkpoint(self):
        s=spec();s['check_points'][0]['wgs84'][0]+=.002
        self.assertFalse(fit_registration(s)['independent_error_gate_passed'])

    def test_bad_controls(self):
        for mutate in ('collinear','reuse','unknown','nan','duplicate'):
            s=spec()
            if mutate=='collinear':
                s['control_points']=s['control_points'][:3]
                for i,p in enumerate(s['control_points']): p['pixel_xy']=[i,i]
            elif mutate=='reuse': s['check_points'][0]=copy.deepcopy(s['control_points'][0])
            elif mutate=='unknown': s['coordinate_system']='GCJ02'
            elif mutate=='nan': s['control_points'][0]['wgs84'][0]=float('nan')
            else: s['control_points'][1]['pixel_xy']=[0,0]
            with self.assertRaises(ValueError, msg=mutate): fit_registration(s)

    def test_files_and_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'controls.json').write_text(json.dumps(spec()))
            graph={'coordinate_system':'local_mask_pixels_xy_top_left',
                   'segments_xy':[[[10,10],[20,20]],[[20,20],[30,10]]], 'ordered_path_xy':None}
            (p/'graph.json').write_text(json.dumps(graph))
            register(p/'graph.json',p/'controls.json',p/'out')
            geo=json.loads((p/'out/registered-segments.geojson').read_text())
            self.assertEqual(geo['type'],'FeatureCollection')
            self.assertEqual(len(geo['features']),2)
            self.assertFalse(geo['features'][0]['properties']['traversal_order_known'])
            self.assertFalse(geo['features'][0]['properties']['verified'])


if __name__=='__main__': unittest.main()
