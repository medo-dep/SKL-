"""Procedural design assets (numpy + OpenCV): crumpled navy backdrop, torn paper card, rounded frame mask/glow.

Generated on first use and cached; nothing is downloaded and there is no licensing to worry about.
"""

import os

import numpy as np

NAVY = (72, 43, 28)  # BGR of #1C2B48
GOLD = (74, 162, 201)  # BGR of #C9A24A


def _cv2():
    import cv2
    return cv2


def _noise(h, w, scale, rng):
    cv2 = _cv2()
    small = rng.standard_normal((max(2, h // scale), max(2, w // scale))).astype(np.float32)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def crumple_shade(w, h, strength=1.0, seed=11):
    """Light/shadow map (float, ~1.0) of crumpled paper: random flat facets, each tilted its own way."""
    cv2 = _cv2()
    rng = np.random.default_rng(seed)
    sw, sh = max(8, w // 4), max(8, h // 4)
    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    shade = np.zeros((sh, sw), np.float32)
    for count, weight in ((90, 1.0), (260, 0.55)):  # big folds plus smaller wrinkles
        pts = rng.uniform(0, 1, (count, 2)) * (sw, sh)
        normals = rng.normal(0, 1, (count, 2))
        best = np.full((sh, sw), np.inf, np.float32)
        idx = np.zeros((sh, sw), np.int32)
        for i, (px, py) in enumerate(pts):
            d = (xx - px) ** 2 + (yy - py) ** 2
            closer = d < best
            best[closer], idx[closer] = d[closer], i
        shade += weight * (normals[idx, 0] * -0.6 + normals[idx, 1] * -0.8)  # light from the top-left
    shade = cv2.resize(shade, (w, h), interpolation=cv2.INTER_LINEAR)
    shade = cv2.GaussianBlur(shade, (0, 0), 1.6) + 0.15 * _noise(h, w, 3, rng)
    return np.clip(1 + 0.09 * strength * shade, 0.6, 1.4)


def crumpled(w, h, base=NAVY, strength=1.0, seed=11):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    vignette = 1 - 0.7 * (((xx - w / 2) / w) ** 2 + ((yy - h / 2) / h) ** 2)
    shade = crumple_shade(w, h, strength, seed) * vignette
    img = np.stack([np.full((h, w), c, np.float32) for c in base], axis=2) * shade[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def torn_paper(cw, ch, lines=6, seed=5):
    """Aged ruled note paper with torn edges, a red margin on the right (RTL) and a strip of tape. BGRA."""
    cv2 = _cv2()
    rng = np.random.default_rng(seed)
    base = np.array([186, 217, 233], np.float32)  # BGR beige #E9D9BA
    img = np.ones((ch, cw, 3), np.float32) * base
    img *= (1 + 0.05 * _noise(ch, cw, 40, rng)[..., None] + 0.03 * _noise(ch, cw, 6, rng)[..., None])
    for _ in range(7):  # coffee / age stains
        mask = np.zeros((ch, cw), np.float32)
        cv2.circle(mask, (int(rng.uniform(0, cw)), int(rng.uniform(0, ch))), int(rng.uniform(40, 160)), 1, -1)
        img *= 1 - 0.07 * cv2.GaussianBlur(mask, (0, 0), 35)[..., None]
    img *= crumple_shade(cw, ch, 0.3, seed + 1)[..., None]
    top, gap = int(ch * 0.2), (ch * 0.75) / max(1, lines)
    for i in range(lines + 1 if lines else 0):
        y = int(top + i * gap)
        cv2.line(img, (int(cw * 0.04), y), (int(cw * 0.96), y), (200, 196, 190), 2, cv2.LINE_AA)
    if lines:
        cv2.line(img, (int(cw * 0.89), int(ch * 0.04)), (int(cw * 0.89), int(ch * 0.96)), (150, 150, 225), 2, cv2.LINE_AA)
    # torn outline: a jagged polygon just inside the card
    pts = []
    for side in range(4):
        n = 140
        walk = 0.0
        for i in range(n):
            t = i / n
            walk = 0.82 * walk + rng.normal(0, 3.2)  # a random walk reads as torn fibre, not a saw
            jag = 8 + abs(walk) + (rng.uniform(0, 1) < 0.03) * rng.uniform(8, 22)
            if side == 0:
                pts.append((t * cw, jag))
            elif side == 1:
                pts.append((cw - jag, t * ch))
            elif side == 2:
                pts.append(((1 - t) * cw, ch - jag))
            else:
                pts.append((jag, (1 - t) * ch))
    alpha = np.zeros((ch, cw), np.uint8)
    cv2.fillPoly(alpha, [np.array(pts, np.int32)], 255, cv2.LINE_AA)
    edge = cv2.GaussianBlur((alpha > 0).astype(np.float32), (0, 0), 6)
    img *= (0.8 + 0.2 * edge)[..., None]  # darker, worn edges
    tape = (int(cw * 0.18), int(ch * 0.0), int(cw * 0.45), int(ch * 0.075))
    overlay = img.copy()
    cv2.rectangle(overlay, tape[:2], tape[2:], (150, 190, 215), -1)
    img = cv2.addWeighted(overlay, 0.55, img, 0.45, 0)
    out = np.dstack([np.clip(img, 0, 255).astype(np.uint8), alpha])
    return out


def with_shadow(card, spread=28, opacity=0.55, offset=(10, 16)):
    """Card (BGRA) on a transparent canvas with a soft drop shadow under it."""
    cv2 = _cv2()
    ch, cw = card.shape[:2]
    W, H = cw + 2 * spread, ch + 2 * spread
    out = np.zeros((H, W, 4), np.float32)
    sh = np.zeros((H, W), np.float32)
    sh[spread + offset[1]:spread + offset[1] + ch, spread + offset[0]:spread + offset[0] + cw] = \
        card[..., 3][:H - spread - offset[1], :W - spread - offset[0]] / 255
    out[..., 3] = cv2.GaussianBlur(sh, (0, 0), spread / 2) * opacity * 255
    a = card[..., 3:4].astype(np.float32) / 255
    region = out[spread:spread + ch, spread:spread + cw]
    region[..., :3] = card[..., :3] * a + region[..., :3] * (1 - a)
    region[..., 3:4] = np.maximum(region[..., 3:4], a * 255)
    return np.clip(out, 0, 255).astype(np.uint8)


def rounded_mask(w, h, r):
    cv2 = _cv2()
    m = np.zeros((h, w), np.uint8)
    cv2.rectangle(m, (r, 0), (w - r, h), 255, -1)
    cv2.rectangle(m, (0, r), (w, h - r), 255, -1)
    for cx, cy in ((r, r), (w - r, r), (r, h - r), (w - r, h - r)):
        cv2.circle(m, (cx, cy), r, 255, -1, cv2.LINE_AA)
    return m


def glow(w, h, r, spread=40, color=(255, 255, 255), strength=0.55):
    """Soft halo around a rounded rectangle (BGRA, sized w+2*spread x h+2*spread)."""
    cv2 = _cv2()
    W, H = w + 2 * spread, h + 2 * spread
    m = np.zeros((H, W), np.uint8)
    m[spread:spread + h, spread:spread + w] = rounded_mask(w, h, r)
    a = cv2.GaussianBlur(m.astype(np.float32), (0, 0), spread / 2.5) * strength
    return np.dstack([np.full((H, W), c, np.uint8) for c in color] + [np.clip(a, 0, 255).astype(np.uint8)])


def ensure(name, builder, cache_dir):
    cv2 = _cv2()
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, name)
    if not os.path.exists(path):
        cv2.imwrite(path, builder())
    return path
