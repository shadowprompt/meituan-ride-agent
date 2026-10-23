"""Generate an artificial map-like color extraction fixture, not real ride data."""
from pathlib import Path
from PIL import Image, ImageDraw

image = Image.new('RGB', (640, 480), '#f4f5f1')
draw = ImageDraw.Draw(image)
for x in range(40, 640, 80):
    draw.line([(x, 0), (x, 480)], fill='#c4c4c4', width=8)
for y in range(40, 480, 80):
    draw.line([(0, y), (640, y)], fill='#c4c4c4', width=8)
draw.line([(120, 360), (280, 360), (280, 120), (200, 120),
           (280, 120), (280, 200), (520, 200)], fill='#00BFA5', width=7)
draw.ellipse((110, 350, 130, 370), fill='#00BFA5')
draw.ellipse((510, 190, 530, 210), fill='#ff4444')
draw.point((600, 420), fill='#00BFA5')
draw.text((15, 15), 'SYNTHETIC - no real location / no GPS', fill='black')
path = Path(__file__).parent/'synthetic-ride.png'
image.save(path)
print(path)
