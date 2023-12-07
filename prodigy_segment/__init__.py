import time 
import base64
from io import BytesIO
from typing import List
import numpy as np
import torch
from PIL import Image, ImageColor, ImageEnhance
from pathlib import Path
from diskcache import Cache
from prodigy.components.preprocess import fetch_media
from prodigy.components.stream import get_stream
from prodigy.core import Arg, recipe, Controller
from prodigy.protocols import ControllerComponentsDict
from prodigy.types import LabelsType, SourceType, TaskType
from prodigy.util import log, msg
from segment_anything import sam_model_registry, SamPredictor
from segment_anything.utils.onnx import SamOnnxModel


HTML = """
<link
  rel="stylesheet"
  href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.1.2/css/all.min.css"
  integrity="sha512-1sCRPdkRXhBV2PBLUdRb4tMg1w2YPf37qatUFeS7zlBy7jJI8Lf4VHwWfZZfpXtYSLy85pkm9GaYVYMfw5BC1A=="
  crossorigin="anonymous"
  referrerpolicy="no-referrer"
/>
<button id="refreshButton" onclick="refreshData()">
  Segment Image
  <i
    id="loadingIcon"
    class="fa-solid fa-spinner fa-spin"
    style="display: none;"
  ></i>
</button>
"""

JS = """
function refreshData() {
  document.querySelector('#loadingIcon').style.display = 'inline-block'
  event_data = {
    example: window.prodigy.content,
  }
  window.prodigy
    .event('segment-anything', event_data)
    .then(updated_example => {
      console.log('Updating Current Example with new data:', updated_example)
      window.prodigy.update(updated_example)
      document.querySelector('#loadingIcon').style.display = 'none'
    })
    .catch(err => {
      console.error('Error in Event Handler:', err)
    })
}
"""


def before_db(examples: List[TaskType]) -> List[TaskType]:
    # Remove all data URIs before storing example in the database
    # also check if the `orig_image` is in there and replace if so
    for eg in examples:
        eg["image"] = eg["orig_image"]
        del eg["orig_image"]
        if eg["image"].startswith("data:"):
            eg["image"] = eg.get("path")
    return examples


def before_db_orig_image(examples: List[TaskType]) -> List[TaskType]:
    # Check if the `orig_image` is in there and replace if so
    for eg in examples: 
        eg["image"] = eg["orig_image"]
        del eg["orig_image"]
    return examples 


def add_orig_images(examples: List[TaskType]) -> List[TaskType]:
    for ex in examples:
        ex['orig_image'] = ex['image']
        yield ex


def pil_to_alpha_mask(pil_img, color="#770"):
    imga = pil_img.convert("RGBA")
    imga = np.asarray(imga) 
    r, g, b, a = np.rollaxis(imga, axis=-1) # split into 4 n x m arrays 
    r_m = r > 10 # binary mask for red channel, True for all non white values
    g_m = g > 10 # binary mask for green channel, True for all non white values
    b_m = b > 10 # binary mask for blue channel, True for all non white values
    # combine the three masks using the binary "or" operation 
    a = a * ((r_m == 1) | (g_m == 1) | (b_m == 1))
    
    # Apply new colors too
    r_new, g_new, b_new = ImageColor.getrgb(color)
    r = np.ones_like(r) * r_new
    g = np.ones_like(g) * g_new
    b = np.ones_like(b) * b_new
    
    # stack the img back together 
    im = Image.fromarray(np.dstack([r, g, b, a]), 'RGBA')
    alpha = im.split()[3]
    alpha = ImageEnhance.Brightness(alpha).enhance(.5)
    im.putalpha(alpha)
    im.save("debug-mask.png")
    return im


def pil_to_base64(pil):
    with BytesIO() as buffered:
        pil.save(buffered, format="JPEG")
        img_str = base64.b64encode(buffered.getvalue())
    return f"data:image/png;base64,{img_str.decode('utf-8')}"


def calculate_masks(box_coordinates: List, predictor: SamPredictor, pil_image: Image):
    input_boxes = torch.tensor([box_coordinates], device=predictor.device)
    transformed_boxes = predictor.transform.apply_boxes_torch(input_boxes, np.array(pil_image).shape[:2])
    masks, _, _ = predictor.predict_torch(
        point_coords=None,
        point_labels=None,
        boxes=transformed_boxes,
        multimask_output=False,
    )
    return masks


def build_onnx_model(sam):
    return SamOnnxModel(sam, return_single_mask=True)


def encode_image(path: str, cache: Cache):
    string_path = str(path)
    if string_path not in cache:
        tic = time.time()
        pil_image = PIL.open(string_path).convert("RGB")
        predictor.set_image(np.array(pil_image))
        image_embedding = predictor.get_image_embedding().cpu().numpy()
        cache[string_path] = image_embedding
        toc = time.time()
        log(f"ENCODE_IMAGE: {img_path=} encoded. Took {round(toc - tic)}s")
    return cache[string_path]


@recipe("segment.cache",
    source=Arg(help="Data to annotate (directory of images, file path or '-' to read from standard input)"),
    checkpoint=Arg(help="Path to model checkpoint"),
    model_type=Arg("--model-type", "-mt", help="Model type to use"),
    cache=Arg("--cache", "-c", help="Location of the diskcache"),
    loader=Arg("--loader", "-lo", help="Loader if source is not directory of images"),
)
def segment_cache(source: SourceType, checkpoint: Path, model_type: str = "default", cache: str = "segment-anything-cache", loader: str = "images"):
    """Cache the segment-anything representations to speed up annotation later."""
    log("RECIPE: Starting recipe `segment.cachefill`", locals())
    if not checkpoint.exists():
        msg.fail(f"Path {checkpoint=} does not exist.", exits=True)
    log("RECIPE: Loading model")
    sam = sam_model_registry[model_type](checkpoint=checkpoint)
    stream = get_stream(
        source,
        loader=loader,
        dedup=True,
        rehash=True,
        input_key="image",
        is_binary=False,
    )
    diskcache = Cache(cache)
    log("RECIPE: About to check the cache.")
    for ex in stream:
        encode_image(ex['image'], cache=diskcache)


@recipe(
    "segment.image.manual",
    # fmt: off
    dataset=Arg(help="Dataset to save annotations to"),
    source=Arg(help="Data to annotate (directory of images, file path or '-' to read from standard input)"),
    checkpoint=Arg(help="Path to model checkpoint"),
    label=Arg("--label", "-l", help="Comma-separated label(s) to annotate or text file with one label per line"),
    loader=Arg("--loader", "-lo", help="Loader if source is not directory of images"),
    exclude=Arg("--exclude", "-e", help="Comma-separated list of dataset IDs whose annotations to exclude"),
    darken=Arg("--darken", "-D", help="Darken image to make boxes stand out more"),
    width=Arg("--width", "-w", help="Default width of the annotation card and space for the image (in px)"),
    no_fetch=Arg("--no-fetch", "-NF", help="Don't fetch images as base64"),
    remove_base64=Arg("--remove-base64", "-R", help="Remove base64-encoded image data before storing example in the DB. (Caution: if enabled, make sure to keep original files!)"),
    model_type=Arg("--model-type", "-mt", help="Model type to use"),
    # fmt: on
)
def segment_image_manual(
    dataset: str,
    source: SourceType,
    label: LabelsType,
    checkpoint: Path,
    loader: str = "images",
    exclude: List[str] = [],
    darken: bool = False,
    width: int = 675,
    no_fetch: bool = False,
    remove_base64: bool = False,
    model_type: str = "default"
) -> ControllerComponentsDict:
    """
    Manually annotate images by drawing rectangular bounding boxes or polygon
    shapes on the image.
    """
    log("RECIPE: Starting recipe `segment.image.manual`", locals())
    if not checkpoint.exists():
        msg.fail(f"Path {checkpoint=} does not exist.", exits=True)

    sam = sam_model_registry[model_type](str(checkpoint))
    predictor = SamPredictor(sam)
    cache_key = ""

    stream = get_stream(
        source,
        loader=loader,
        dedup=True,
        rehash=True,
        input_key="image",
        is_binary=False,
    )
    if not no_fetch and loader != "image-server":
        stream.apply(fetch_media, stream=stream, input_keys=["image"])

    # Because we overwrite the original image when we apply the mask we have to store it separately
    # These original images are properly restored in the before_db callbacks later
    stream.apply(add_orig_images)

    # These colors are used for bounding boxes _and_ image masks
    colors = ["#00ffff", "#ff00ff", "#00ff7f", "#ff6347", "#00bfff",
              "#ffa500", "#ff69b4", "#7fffd4", "#ffd700", "#ffdab9", "#adff2f", 
              "#d2b48c", "#dcdcdc", "#ffff00", ]
    label_2_color = {lab: colors[i] for i, lab in enumerate(label)}

    def event_hook(ctrl: Controller, *, example: dict):
        nonlocal cache_key
        log(f"RECIPE: Event hook called input_hash={example['_input_hash']}.")
        if not example.get("spans", []):
            log("RECIPE: Example had no spans. Returning example early.")
            if "orig_image" in example:
                example["image"] = example["orig_image"]
            return example 

        # Load the original image and keep it safe for now
        base64_img = example['orig_image'][example['orig_image'].find("base64,") + 7:]
        pil_image = Image.open(BytesIO(base64.b64decode(base64_img))).convert("RGBA")
        
        # Setting a new image is expensive, so we only want to do this once per image
        if cache_key != example['path']:
            predictor.set_image(np.array(pil_image.convert("RGB")))
            log("RECIPE: New image. Running preprocessing for Sam Predictor.")
            cache_key = example["path"]

        # Generate the masks via PyTorch. Note that `.apply_boxes_torch` is a must!
        box_coordinates = [
            [s['x'], s['y'], s['x'] + s['width'], s['y'] + s['height']] for s in example['spans']
        ]
        masks = calculate_masks(box_coordinates, predictor, pil_image)
        
        
        # Update original image to show mask and add base64 mask to span.
        new_spans = []
        log(f"RECIPE: There are {len(example['spans'])} spans selected.")
        for i, mask in enumerate(masks):
            h, w = mask.shape[-2:]
            np_mask = (np.array(mask).astype(int).reshape(h, w)  * 255).astype(np.uint8)
            color = label_2_color[example['spans'][i]['label']]
            alpha_mask = pil_to_alpha_mask(Image.fromarray(np_mask), color=color)
            # Paste the mask on top of original image
            pil_image.paste(alpha_mask, (0,0), mask=alpha_mask)
            new_span = example['spans'][i]
            new_span['mask'] = pil_to_base64(Image.fromarray(np_mask))
            new_spans.append(new_span)
        

        example["image"] = pil_to_base64(pil_image.convert("RGB"))
        example["spans"] = new_spans
        log("RECIPE: segment anything ran.")
        return example

    blocks = [{"view_id": "image_manual"}, {"view_id": "html", "html_template": HTML}]

    return {
        "view_id": "blocks",
        "dataset": dataset,
        "stream": stream,
        "before_db": before_db if remove_base64 else before_db_orig_image,
        "exclude": exclude,
        "config": {
            "labels": label,
            "blocks": blocks,
            "darken_image": 0.3 if darken else 0,
            "exclude_by": "input",
            "auto_count_stream": True,
            "javascript": JS,
            "image_manual_modes": ["rect"],
            "custom_theme": {
                "labels": label_2_color,
                "cardMaxWidth": width
            }
        },
        "event_hooks": {
            "segment-anything": event_hook
        }
    }
