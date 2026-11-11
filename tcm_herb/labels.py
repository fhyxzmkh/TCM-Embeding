"""Explicit label mapping: never infer labels independently for the two domains."""

HERBS = [
    ("Alpinia katsumadai Hayata", "草豆蔻"),
    ("Amomi Fructus", "砂仁"),
    ("Amomi Fructus Rotundus", "白豆蔻"),
    ("Chaenomelis Fructus", "木瓜"),
    ("Cornifructus", "山茱萸"),
    ("Crataegi Fructus", "山楂"),
    ("Foeniculi Fructus", "小茴香"),
    ("Forsythiae Fructus", "连翘"),
    ("Gardeniae Fructus", "栀子"),
    ("Kochiae Fructus", "地肤子"),
    ("Lycii Fructus", "枸杞子"),
    ("Mume Fructus", "乌梅"),
    ("Persicae Semen", "桃仁"),
    ("Psoralea corylifolia", "补骨脂"),
    ("Rosae Laevigatae Frucyus", "金樱子"),
    ("Rubi Fructus", "覆盆子"),
    ("Schisandrae Chinensis Fructus", "五味子"),
    ("Semen Armeniacae Amarum", "苦杏仁"),
    ("Toosendan Fructus", "川楝子"),
    ("Trichosanhes kirilowii", "瓜蒌类（按原数据标签）"),
]

CLASS_NAMES = [name for name, _ in HERBS]
CHINESE_NAMES = [name for _, name in HERBS]
ALIASES = {"doukou": "Amomi Fructus Rotundus"}


def label_id(folder_name):
    canonical = ALIASES.get(folder_name, folder_name)
    if canonical not in CLASS_NAMES:
        raise ValueError(f"Unmapped herb directory: {folder_name!r}")
    return CLASS_NAMES.index(canonical)


def label_metadata():
    return [
        {"id": index, "name": latin, "name_zh": chinese}
        for index, (latin, chinese) in enumerate(HERBS)
    ]
