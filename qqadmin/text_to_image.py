import os
import tempfile
import traceback

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

DEFAULT_FONT_SIZE = 16
DEFAULT_TEXT_COLOR = (30, 30, 30)
DEFAULT_BACKGROUND_COLOR = (255, 255, 255)
DEFAULT_PADDING = 40
DEFAULT_LINE_SPACING = 10
FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]

def find_font():
    for path in FONT_PATHS:
        if os.path.exists(path):
            return path
    return None

def text_to_image(text: str, font_size: int = DEFAULT_FONT_SIZE, 
                 text_color: tuple = DEFAULT_TEXT_COLOR,
                 background_color: tuple = DEFAULT_BACKGROUND_COLOR,
                 padding: int = DEFAULT_PADDING,
                 line_spacing: int = DEFAULT_LINE_SPACING) -> str:
    if not PIL_AVAILABLE:
        return None

    try:
        font_path = find_font()
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            font = ImageFont.load_default()

        lines = text.split('\n')
        draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        
        max_width = 0
        total_height = 0
        
        for line in lines:
            if line.strip():
                bbox = draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                max_width = max(max_width, line_width)
                total_height += line_height + line_spacing
        
        total_height = max(total_height - line_spacing, font_size)
        
        image_width = max_width + padding * 2
        image_height = total_height + padding * 2
        
        image = Image.new('RGB', (image_width, image_height), background_color)
        draw = ImageDraw.Draw(image)
        
        y = padding
        for line in lines:
            if line.strip():
                bbox = draw.textbbox((0, 0), line, font=font)
                line_height = bbox[3] - bbox[1]
                draw.text((padding, y), line, font=font, fill=text_color)
                y += line_height + line_spacing
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            temp_path = f.name
        
        image.save(temp_path, format='PNG')
        return temp_path
    
    except Exception as e:
        print(f"text_to_image error: {e}")
        print(traceback.format_exc())
        return None

def is_text_to_image_available() -> bool:
    return PIL_AVAILABLE and find_font() is not None