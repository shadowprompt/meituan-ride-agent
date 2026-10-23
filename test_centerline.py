import tempfile
from pathlib import Path
import unittest

import numpy as np
from PIL import Image
from centerline import thin, analyze, process, pixel_graph


class CenterlineTests(unittest.TestCase):
    def test_open_line(self):
        a = np.zeros((20,30), bool)
        a[10,3:25] = True
        report = analyze(a)
        self.assertEqual(len(report['endpoints_xy']), 2)
        self.assertEqual(len(report['ordered_path_xy']), 22)
        self.assertEqual(report['cycle_rank'], 0)
        self.assertFalse(report['verified'])

    def test_diagonal_and_corner(self):
        a = np.eye(8, dtype=bool)
        self.assertEqual(analyze(a)['component_count'], 1)
        self.assertEqual(len(analyze(a)['ordered_path_xy']), 8)
        a = np.zeros((8,8), bool)
        a[1,1:5] = True
        a[1:6,4] = True
        self.assertEqual(analyze(a)['cycle_rank'], 0)
        self.assertIsNotNone(analyze(a)['ordered_path_xy'])

    def test_thick_line(self):
        a = np.zeros((30,40), bool)
        a[10:17,5:35] = True
        skeleton, _ = thin(a)
        self.assertLess(int(skeleton.sum()), int(a.sum()))
        self.assertIsNotNone(analyze(skeleton)['ordered_path_xy'])
        second, _ = thin(skeleton)
        np.testing.assert_array_equal(second, skeleton)

    def test_branch(self):
        a = np.zeros((20,20), bool)
        a[3:17,10] = True
        a[8,5:16] = True
        report = analyze(a)
        self.assertIsNone(report['ordered_path_xy'])
        self.assertTrue(report['junction_pixels_xy'])
        self.assertEqual(len(report['segments_xy']), 4)
        # Every graph edge is represented exactly once, including branch junctions.
        self.assertEqual(sum(len(s)-1 for s in report['segments_xy']), report['edge_count'])

    def test_loop(self):
        a = np.zeros((12,12), bool)
        a[2,2:10] = a[9,2:10] = True
        a[2:10,2] = a[2:10,9] = True
        report = analyze(a)
        self.assertEqual(report['cycle_rank'], 1)
        self.assertIsNone(report['ordered_path_xy'])
        self.assertEqual(report['segments_xy'][0][0], report['segments_xy'][0][-1])

    def test_disconnected_and_empty(self):
        a = np.zeros((20,20), bool)
        a[2,2:8] = True
        a[15,2:8] = True
        report = analyze(a)
        self.assertEqual(report['component_count'], 2)
        self.assertIsNone(report['ordered_path_xy'])
        report = analyze(np.zeros((4,4), bool))
        self.assertEqual(report['status'], 'no_detection')

    def test_border_and_singleton(self):
        a = np.zeros((10,10), bool)
        a[0,:] = True
        skeleton, _ = thin(a)
        self.assertEqual(int(skeleton.sum()), 10)
        self.assertIsNotNone(analyze(skeleton)['ordered_path_xy'])
        self.assertIsNone(analyze(np.ones((1,1), bool))['ordered_path_xy'])

    def test_cli_artifacts_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, out = Path(tmp)/'mask.png', Path(tmp)/'out'
            a = np.zeros((20,20), np.uint8)
            a[5,2:18] = 255
            Image.fromarray(a).save(p)
            result = process(p, out)
            self.assertIsNotNone(result['ordered_path_xy'])
            for name in ('skeleton.png','graph-overlay.png','pixel-graph.json'):
                self.assertTrue((out/name).exists())
            Image.new('RGB', (20,20)).save(p)
            with self.assertRaises(ValueError):
                process(p, out)
            Image.new('L', (20,20), 128).save(p)
            with self.assertRaises(ValueError):
                process(p, out)


if __name__ == '__main__':
    unittest.main()
