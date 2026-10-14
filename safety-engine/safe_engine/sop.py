"""Guidance corpus (SOP clauses) and shelters.

IMPORTANT: the clauses below are short *paraphrases* of widely published
public flood / landslide / cyclone safety advice (NDMA public awareness
material, US NWS "Turn Around Don't Drown"). Before deployment, replace them
with the verbatim text of the applicable NDMA / SDMA / district SOPs and have
the Hindi and Tamil versions reviewed by native speakers.
"""
from __future__ import annotations

import json
import os

import numpy as np

SOP = {
    "SOP-FLOOD-01": {"hazard": "flood", "critical": True,
        "en": "Do not drive, ride or walk through flood water. Even 15 cm of moving water can knock a person down and about 30 cm can float a car.",
        "hi": "बाढ़ के पानी में वाहन न चलाएँ, न सवारी करें और न पैदल चलें। बहता हुआ 15 सेमी पानी भी व्यक्ति को गिरा सकता है और लगभग 30 सेमी पानी कार को बहा सकता है।",
        "ta": "வெள்ள நீரில் வாகனம் ஓட்டவோ, நடக்கவோ வேண்டாம். ஓடும் 15 செ.மீ. நீர் கூட ஒருவரைக் கீழே தள்ளலாம்; சுமார் 30 செ.மீ. நீர் காரை மிதக்கச் செய்யலாம்."},
    "SOP-FLOOD-02": {"hazard": "flood", "critical": True,
        "en": "Move to higher ground early. Avoid underpasses, subways and low-lying roads during heavy rain.",
        "hi": "समय रहते ऊँचे स्थान पर जाएँ। भारी बारिश के दौरान अंडरपास, सबवे और निचली सड़कों से बचें।",
        "ta": "முன்கூட்டியே உயரமான இடத்திற்குச் செல்லுங்கள். கனமழையின் போது சுரங்கப்பாதைகள் மற்றும் தாழ்வான சாலைகளைத் தவிர்க்கவும்."},
    "SOP-FLOOD-03": {"hazard": "flood", "critical": False,
        "en": "Switch off electricity at the main switch if water enters your home, and keep drinking water, medicines, a torch and a charged phone ready.",
        "hi": "यदि घर में पानी आए तो मुख्य स्विच से बिजली बंद करें, और पीने का पानी, दवाइयाँ, टॉर्च और चार्ज किया हुआ फ़ोन तैयार रखें।",
        "ta": "வீட்டிற்குள் நீர் புகுந்தால் பிரதான சுவிட்சில் மின்சாரத்தை அணைக்கவும்; குடிநீர், மருந்துகள், டார்ச் மற்றும் சார்ஜ் செய்த கைபேசியை தயாராக வைத்திருங்கள்."},
    "SOP-LS-01": {"hazard": "landslide", "critical": True,
        "en": "Avoid hill roads and steep slopes during and after intense rain. Watch for falling stones, cracks or muddy water and leave the area immediately if you see them.",
        "hi": "तेज़ बारिश के दौरान और बाद में पहाड़ी सड़कों और खड़ी ढलानों से बचें। गिरते पत्थर, दरारें या मटमैला पानी दिखे तो तुरंत क्षेत्र छोड़ दें।",
        "ta": "கனமழையின் போதும் பின்னரும் மலைச் சாலைகள் மற்றும் செங்குத்தான சரிவுகளைத் தவிர்க்கவும். கற்கள் விழுதல், விரிசல்கள் அல்லது சேற்று நீர் தென்பட்டால் உடனே அந்த இடத்தை விட்டு வெளியேறுங்கள்."},
    "SOP-STORM-01": {"hazard": "storm", "critical": True,
        "en": "During strong winds stay indoors away from windows; two-wheelers should not travel. Keep away from trees, hoardings and fallen power lines.",
        "hi": "तेज़ हवाओं के दौरान खिड़कियों से दूर घर के अंदर रहें; दोपहिया वाहन यात्रा न करें। पेड़ों, होर्डिंग और गिरे हुए बिजली के तारों से दूर रहें।",
        "ta": "பலத்த காற்றின் போது ஜன்னல்களிலிருந்து விலகி வீட்டிற்குள் இருங்கள்; இருசக்கர வாகனங்கள் பயணம் செய்ய வேண்டாம். மரங்கள், விளம்பரப் பலகைகள் மற்றும் அறுந்த மின்கம்பிகளிலிருந்து விலகி இருங்கள்."},
    "SOP-GEN-01": {"hazard": "any", "critical": True,
        "en": "Follow instructions from district authorities and official alerts. In an emergency call 112.",
        "hi": "ज़िला प्रशासन और आधिकारिक अलर्ट के निर्देशों का पालन करें। आपात स्थिति में 112 पर कॉल करें।",
        "ta": "மாவட்ட நிர்வாகம் மற்றும் அதிகாரப்பூர்வ எச்சரிக்கைகளின் வழிகாட்டுதல்களைப் பின்பற்றுங்கள். அவசரநிலையில் 112 ஐ அழைக்கவும்."},
}


def sop_for(hazard: str, lang: str = "en") -> list[dict]:
    out = []
    for sid, c in SOP.items():
        if c["hazard"] in (hazard, "any"):
            out.append({"id": sid, "hazard": c["hazard"], "critical": c["critical"], "text": c.get(lang) or c["en"], "lang": lang if lang in c else "en"})
    return out


def synthetic_shelters(terrain, n: int = 14, seed: int = 5) -> list[dict]:
    """Relief-shelter locations on high, well-drained ground (SYNTHETIC for the demo region)."""
    rng = np.random.default_rng(seed)
    ok = (terrain.hand > 4) & (terrain.fill_depth < 0.05) & (terrain.landslide_s < 0.2)
    rs, cs = np.nonzero(ok)
    pick = rng.choice(len(rs), size=min(n, len(rs)), replace=False)
    kinds = ["Government school", "Community hall", "College campus", "Marriage hall", "Stadium"]
    out = []
    for k, i in enumerate(pick):
        x, y = terrain.cell_center(rs[i], cs[i])
        out.append({"id": f"S{k + 1:03d}", "name": f"Relief shelter {k + 1} ({kinds[k % len(kinds)]})", "x": x, "y": y,
                    "capacity": int(rng.integers(150, 900)), "synthetic": True})
    return out


def load_shelters(path: str, frame) -> list[dict]:
    """GeoJSON FeatureCollection of points with properties name, capacity."""
    with open(path) as fh:
        fc = json.load(fh)
    out = []
    for k, f in enumerate(fc["features"]):
        lon, lat = f["geometry"]["coordinates"][:2]
        x, y = frame.to_xy(lon, lat)
        p = f.get("properties", {})
        out.append({"id": p.get("id", f"S{k + 1:03d}"), "name": p.get("name", f"Shelter {k + 1}"), "x": x, "y": y,
                    "capacity": p.get("capacity"), "synthetic": False})
    return out
