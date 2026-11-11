"""Shared architecture, preprocessing and manifest-backed datasets."""

import json
from pathlib import Path

import torch
from PIL import Image
from torch.utils.data import Dataset
from torchvision import models, transforms

MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)


def build_model(classes=20, pretrained=False):
    weights = models.MobileNet_V2_Weights.DEFAULT if pretrained else None
    return models.mobilenet_v2(weights=weights) if classes == 1000 else _classifier_model(classes, weights)


def _classifier_model(classes, weights):
    model = models.mobilenet_v2(weights=weights)
    model.classifier[1] = torch.nn.Linear(model.last_channel, classes)
    return model


def image_transform(size=160, training=False):
    operations = [transforms.Resize((size, size), antialias=True)]
    if training:
        operations += [
            transforms.RandomHorizontalFlip(),
            transforms.RandomAffine(degrees=15, translate=(0.05, 0.05), scale=(0.85, 1.05), fill=255),
            transforms.ColorJitter(brightness=0.15, contrast=0.15, saturation=0.12, hue=0.015),
        ]
    return transforms.Compose(operations + [transforms.ToTensor(), transforms.Normalize(MEAN, STD)])


class HerbDataset(Dataset):
    def __init__(self, manifest_path, split, size=160, training=False):
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.records = [record for record in self.manifest["records"] if record["split"] == split]
        if not self.records:
            raise ValueError(f"Empty split: {split}")
        self.transform = image_transform(size, training)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        record = self.records[index]
        with Image.open(self.manifest_path.parent / record["cache"]) as image:
            tensor = self.transform(image.convert("RGB"))
        return tensor, record["label"], index


def load_checkpoint(path, device="cpu"):
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if checkpoint["architecture"] != "mobilenet_v2":
        raise ValueError("Unsupported checkpoint architecture.")
    model = build_model(len(checkpoint["labels"]))
    model.load_state_dict(checkpoint["state_dict"])
    return model.to(device).eval(), checkpoint


def preprocessing_metadata(size, cache_size=320):
    return {
        "color": "RGB", "exif_transpose": True,
        "letterbox": {"size": cache_size, "fill": [255, 255, 255], "center": [0.5, 0.5], "interpolation": "PIL bilinear"},
        "resize": [size, size], "resize_interpolation": "PIL bilinear",
        "layout": "NCHW", "dtype": "float32", "scale": "pixel / 255.0",
        "mean": list(MEAN), "std": list(STD),
        "output": "logits; softmax yields closed-set scores, not calibrated confidence",
    }
