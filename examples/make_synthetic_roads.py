"""Artificial OSM-format fixture matching the synthetic screenshot. Not OSM data."""
from pathlib import Path
import xml.etree.ElementTree as ET

root = ET.Element('osm',version='0.6',generator='synthetic-demo')
# Independent ways share exact node IDs at the branch.
lines = [[(120,360),(280,360),(280,200),(280,120),(200,120)],[(280,200),(520,200)]]
ids={}
for line in lines:
    # Dense nodes avoid endpoint snapping artifacts in this baseline.
    for a,b in zip(line,line[1:]):
        count=max(abs(b[0]-a[0]),abs(b[1]-a[1]))//5
        for i in range(count+1):
            p=(round(a[0]+(b[0]-a[0])*i/count),round(a[1]+(b[1]-a[1])*i/count))
            if p not in ids:
                identifier=str(len(ids)+1);ids[p]=identifier
                ET.SubElement(root,'node',id=identifier,lon=str(p[0]*.00001),lat=str(-p[1]*.00001))
for index,line in enumerate(lines):
    way=ET.SubElement(root,'way',id=str(index+1));points=[]
    for a,b in zip(line,line[1:]):
        count=max(abs(b[0]-a[0]),abs(b[1]-a[1]))//5
        part=[(round(a[0]+(b[0]-a[0])*i/count),round(a[1]+(b[1]-a[1])*i/count)) for i in range(count+1)]
        points.extend(part if not points else part[1:])
    for p in points:ET.SubElement(way,'nd',ref=ids[p])
    ET.SubElement(way,'tag',k='highway',v='residential')
    ET.SubElement(way,'tag',k='name',v='Synthetic road '+str(index+1))
out=Path(__file__).parent/'synthetic-roads.osm'
ET.ElementTree(root).write(out,encoding='utf-8',xml_declaration=True)
print(out)
