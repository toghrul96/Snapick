import os, sys, subprocess, tempfile, shutil
from urllib.parse import urlparse

# Where we store the user's chosen save folder between sessions
CONFIG_FILE = os.path.expanduser("~/.config/snapick_dir.txt")
DEFAULT_DIR  = os.path.expanduser("~/Pictures/Screenshots")


def get_save_dir():
    """Read the last-used save directory, fall back to default if missing/deleted."""
    if os.path.exists(CONFIG_FILE):
        d = open(CONFIG_FILE).read().strip()
        if os.path.isdir(d):
            return d
    return DEFAULT_DIR


def set_save_dir(d):
    """Persist the chosen save directory so it survives restarts."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    open(CONFIG_FILE, "w").write(d)


def next_filename(directory):
    """Return the next available Screenshot_N.png path in the given directory."""
    os.makedirs(directory, exist_ok=True)
    i = 1
    while True:
        p = os.path.join(directory, f"Screenshot_{i}.png")
        if not os.path.exists(p):
            return p
        i += 1


def notify(title, msg):
    """Fire a desktop notification. Silently ignored if notify-send isn't installed."""
    try:
        subprocess.Popen(["notify-send", title, msg])
    except:
        pass


def copy_to_clipboard(path):
    """Push a PNG file into the Wayland clipboard via wl-copy."""
    try:
        with open(path, "rb") as f:
            subprocess.run(["wl-copy", "--type", "image/png"], input=f.read(), check=True)
        return True
    except:
        return False


def pick_savepath():
    """Open a GTK file-save dialog via zenity. Returns the chosen file path or None if cancelled.
    Using --save mode allows the user to pick an existing file and overwrite it.
    Pre-fills the next auto-numbered filename so the _1 _2 _3 sequence is preserved by default."""
    default = next_filename(get_save_dir())
    r = subprocess.run(
        ["zenity", "--file-selection", "--save",
         "--confirm-overwrite",
         "--title=Save screenshot",
         "--file-filter=PNG files | *.png",
         f"--filename={default}"],
        capture_output=True, text=True)
    if r.returncode == 0:
        path = r.stdout.strip()
        # Ensure the file always ends with .png
        if not path.lower().endswith(".png"):
            path += ".png"
        return path
    return None


# -- Screen capture via XDG Desktop Portal -------------------------------------
def grab_screen():
    """
    Capture the full screen using the XDG Screenshot portal over D-Bus.
    This works on Wayland without any special permissions. The portal might
    briefly flash a system dialog depending on your compositor settings.
    Returns a path to a temporary PNG file - caller is responsible for cleanup.
    """
    import dbus, dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop  = GLib.MainLoop()
    bus   = dbus.SessionBus()
    proxy = bus.get_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
    iface = dbus.Interface(proxy, 'org.freedesktop.portal.Screenshot')

    result_uri = [None]  # list so the nested callback can write to it

    def on_response(response, results):
        # response == 0 means success; anything else is cancel/error
        if response == 0 and 'uri' in results:
            result_uri[0] = str(results['uri'])
        loop.quit()

    req_path = iface.Screenshot('', {'interactive': dbus.Boolean(False)})
    req_obj  = bus.get_object('org.freedesktop.portal.Desktop', req_path)
    req_obj.connect_to_signal('Response', on_response)

    # Bail out after 15 seconds in case the portal never responds
    GLib.timeout_add_seconds(15, loop.quit)
    loop.run()

    if not result_uri[0]:
        raise RuntimeError("Screen capture failed or was cancelled.")

    # Copy out of the portal's temp location before it gets cleaned up
    src = urlparse(result_uri[0]).path
    tmp = tempfile.mktemp(suffix=".png")
    shutil.copy2(src, tmp)
    return tmp


# -- Main pygame UI -------------------------------------------------------------
def run_ui(screen_png):
    """
    Full-screen overlay UI. The user drags to select a region, optionally
    annotates it, then saves/copies or cancels.

    Returns (action, data) where action is one of:
        "save" | "saveas" | "copy" | None
    and data is (pil_full, draw_surf, sel_rect) or None.
    """
    import pygame
    from PIL import Image

    pygame.init()
    pygame.font.init()

    # Load the screenshot and build a dimmed version for the overlay effect.
    # The dimmed version is shown everywhere except inside the selection box.
    pil_full   = Image.open(screen_png).convert("RGB")
    W, H       = pil_full.size
    overlay    = Image.new("RGBA", (W, H), (0, 0, 0, 110))
    pil_dimmed = Image.alpha_composite(pil_full.convert("RGBA"), overlay).convert("RGB")

    # Three surfaces: dimmed background, crisp selection area, annotation layer
    screen    = pygame.display.set_mode((W, H), pygame.NOFRAME)
    surf_dim  = pygame.image.fromstring(pil_dimmed.tobytes(), (W, H), "RGB")
    surf_full = pygame.image.fromstring(pil_full.tobytes(),   (W, H), "RGB")
    draw_surf = pygame.Surface((W, H), pygame.SRCALPHA)
    draw_surf.fill((0, 0, 0, 0))  # fully transparent to start

    clock = pygame.time.Clock()

    try:    font = pygame.font.SysFont("DejaVu Sans", 14)
    except: font = pygame.font.Font(None, 18)

    # UI colours
    COL_SEL_EDGE  = (0,   153, 255)   # selection border / handle outline
    COL_HANDLE    = (255, 255, 255)   # handle fill
    COL_HANDLE_BD = (0,   153, 255)   # handle border
    COL_TB_BG     = (28,  28,  28)    # toolbar background
    COL_TB_TEXT   = (235, 235, 235)   # toolbar label colour
    COL_HOVER     = (60,  60,  60)    # button hover state
    COL_ACTIVE    = (0,   120, 215)   # active tool highlight

    BTN_PAD = 10          # horizontal padding inside each toolbar button
    BTN_H   = 30          # toolbar button height
    TB_H    = BTN_H + 8   # total toolbar strip height
    HANDLE  = 8           # resize handle square size (pixels)

    # Toolbar buttons in display order
    BUTTONS = [
        ("Draw",    "draw"),
        ("Erase",   "erase"),
        ("Undo",    "undo"),
        ("Copy",    "copy"),
        ("Save",    "save"),
        ("Save As", "saveas"),
        ("X",       "cancel"),
    ]

    # -- Interaction state machine ----------------------------------------------
    # States:
    #   "selecting"  – user is dragging out the initial rectangle
    #   "selected"   – rectangle is set, waiting for the next action
    #   "resizing"   – user is dragging one of the 8 edge/corner handles
    #   "moving"     – user is dragging the selection box itself
    #   "drawing"    – freehand draw mode active
    #   "erasing"    – eraser mode active
    state      = "selecting"
    sel_start  = None
    sel_rect   = None   # the active selection as a pygame.Rect
    tool       = None   # "draw" | "erase" | None

    draw_color = (255, 50, 50)   # default annotation colour (red)
    DRAW_W     = 3               # pen stroke width
    ERASE_R    = 12              # eraser radius

    strokes  = []   # completed strokes: [(mode, color, [pts]), ...]
    cur_pts  = []   # points for the stroke currently being drawn

    # Drag state - only meaningful during "resizing" and "moving"
    drag_handle = None   # one of "tl","tc","tr","ml","mr","bl","bc","br"
    drag_offset = None   # (mouse_x - rect.x, mouse_y - rect.y) at drag start
    orig_rect   = None   # snapshot of sel_rect at the moment dragging began

    # -- Helper: build selection rect from two corner points -------------------
    def sel_from(a, b):
        x = min(a[0], b[0])
        y = min(a[1], b[1])
        return pygame.Rect(x, y, abs(a[0]-b[0]), abs(a[1]-b[1]))

    # -- Helper: 8 resize handle rects for a given selection rect --------------
    def handle_rects(r):
        cx, cy = r.centerx, r.centery
        hs = HANDLE
        return {
            "tl": pygame.Rect(r.left  - hs//2, r.top    - hs//2, hs, hs),
            "tc": pygame.Rect(cx      - hs//2, r.top    - hs//2, hs, hs),
            "tr": pygame.Rect(r.right - hs//2, r.top    - hs//2, hs, hs),
            "ml": pygame.Rect(r.left  - hs//2, cy       - hs//2, hs, hs),
            "mr": pygame.Rect(r.right - hs//2, cy       - hs//2, hs, hs),
            "bl": pygame.Rect(r.left  - hs//2, r.bottom - hs//2, hs, hs),
            "bc": pygame.Rect(cx      - hs//2, r.bottom - hs//2, hs, hs),
            "br": pygame.Rect(r.right - hs//2, r.bottom - hs//2, hs, hs),
        }

    # Cursor to show when dragging each handle
    HANDLE_CURSORS = {
        "tl": pygame.SYSTEM_CURSOR_SIZENWSE,
        "br": pygame.SYSTEM_CURSOR_SIZENWSE,
        "tr": pygame.SYSTEM_CURSOR_SIZENESW,
        "bl": pygame.SYSTEM_CURSOR_SIZENESW,
        "tc": pygame.SYSTEM_CURSOR_SIZENS,
        "bc": pygame.SYSTEM_CURSOR_SIZENS,
        "ml": pygame.SYSTEM_CURSOR_SIZEWE,
        "mr": pygame.SYSTEM_CURSOR_SIZEWE,
    }

    # -- Helper: compute new rect after moving a resize handle -----------------
    def apply_handle_drag(handle, orig, mx, my, drag_ox, drag_oy):
        """
        Returns a new pygame.Rect with the appropriate edge/corner moved to
        the current mouse position.

        Important: we deliberately avoid assigning r.left / r.top on a Rect
        directly because pygame keeps width/height fixed when you do that,
        which means the opposite edge moves too and the rect just translates
        instead of resizing. Working with raw integers sidesteps this.
        """
        left   = orig.left
        top    = orig.top
        right  = orig.right
        bottom = orig.bottom

        if handle == "tl":
            left = min(mx, right - 4);  top    = min(my, bottom - 4)
        elif handle == "tc":
            top    = min(my, bottom - 4)
        elif handle == "tr":
            right  = max(mx, left + 4); top    = min(my, bottom - 4)
        elif handle == "ml":
            left   = min(mx, right - 4)
        elif handle == "mr":
            right  = max(mx, left  + 4)
        elif handle == "bl":
            left   = min(mx, right - 4); bottom = max(my, top + 4)
        elif handle == "bc":
            bottom = max(my, top + 4)
        elif handle == "br":
            right  = max(mx, left  + 4); bottom = max(my, top + 4)

        # Don't let the rect go off screen
        left   = max(0, left);   top    = max(0, top)
        right  = min(W, right);  bottom = min(H, bottom)
        return pygame.Rect(left, top, right - left, bottom - top)

    # -- Helper: build toolbar button rects anchored to the selection ----------
    def toolbar_rects(sel):
        # Prefer below the selection; flip above if it would go off screen
        tb_y = sel.bottom + 4
        if tb_y + TB_H > H - 4:
            tb_y = sel.top - TB_H - 4
        x = sel.left
        rects = []
        for label, action in BUTTONS:
            w = font.size(label)[0] + BTN_PAD * 2
            rects.append((label, action, pygame.Rect(x, tb_y, w, BTN_H)))
            x += w + 2
        return rects, tb_y

    # -- Helper: punch a transparent circle into the draw surface (eraser) -----
    def erase_circle(surf, cx, cy, r):
        for px in range(max(0, cx - r), min(W, cx + r + 1)):
            for py in range(max(0, cy - r), min(H, cy + r + 1)):
                if (px - cx)**2 + (py - cy)**2 <= r * r:
                    surf.set_at((px, py), (0, 0, 0, 0))

    # -- Helper: rebuild draw_surf from the strokes list (used by Undo) --------
    def redraw_strokes():
        draw_surf.fill((0, 0, 0, 0))
        for mode, col, pts in strokes:
            if mode == "draw" and len(pts) > 1:
                for i in range(1, len(pts)):
                    pygame.draw.line(draw_surf, (*col, 255), pts[i-1], pts[i], DRAW_W)
            elif mode == "erase":
                for pt in pts:
                    erase_circle(draw_surf, pt[0], pt[1], ERASE_R)

    # -- Helper: returns which handle the mouse is over, or None ---------------
    def hit_handle(mx, my):
        if not sel_rect:
            return None
        for name, r in handle_rects(sel_rect).items():
            # Slightly inflate hit area so it's easier to grab
            if r.inflate(4, 4).collidepoint(mx, my):
                return name
        return None

    # -- Main event / render loop -----------------------------------------------
    running = True
    while running:
        clock.tick(60)
        mx, my = pygame.mouse.get_pos()

        # Cursor shape reflects the current interaction mode
        if state == "selecting":
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_CROSSHAIR)
        elif state == "resizing" and drag_handle:
            pygame.mouse.set_cursor(HANDLE_CURSORS.get(drag_handle, pygame.SYSTEM_CURSOR_ARROW))
        elif state == "moving":
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_SIZEALL)
        elif state in ("drawing", "erasing"):
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_CROSSHAIR)
        elif state == "selected" and sel_rect:
            h = hit_handle(mx, my)
            if h:
                pygame.mouse.set_cursor(HANDLE_CURSORS.get(h, pygame.SYSTEM_CURSOR_ARROW))
            elif sel_rect.collidepoint(mx, my):
                pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_SIZEALL)
            else:
                pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_CROSSHAIR)
        else:
            pygame.mouse.set_cursor(pygame.SYSTEM_CURSOR_ARROW)

        # -- Events ------------------------------------------------------------
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return None, None

            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    if state in ("drawing", "erasing"):
                        # Drop back to selected without discarding the selection
                        tool  = None
                        state = "selected"
                    else:
                        return None, None
                if ev.key == pygame.K_s and (ev.mod & pygame.KMOD_CTRL):
                    if sel_rect and sel_rect.width > 2:
                        if ev.mod & pygame.KMOD_SHIFT:
                            return "saveas", (pil_full, draw_surf, sel_rect)
                        return "save", (pil_full, draw_surf, sel_rect)

            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:

                if state == "selecting":
                    # Start drawing the selection rectangle
                    sel_start = (mx, my)
                    sel_rect  = None

                elif state == "selected":
                    # Check toolbar first, then handles, then move, then reselect
                    btns, _ = toolbar_rects(sel_rect)
                    clicked = next((a for l, a, r in btns if r.collidepoint(mx, my)), None)

                    if   clicked == "cancel": return None, None
                    elif clicked == "save":   return "save",   (pil_full, draw_surf, sel_rect)
                    elif clicked == "saveas": return "saveas", (pil_full, draw_surf, sel_rect)
                    elif clicked == "copy":   return "copy",   (pil_full, draw_surf, sel_rect)
                    elif clicked == "undo":
                        if strokes:
                            strokes.pop()
                            redraw_strokes()
                    elif clicked == "draw":
                        tool  = "draw"
                        state = "drawing"
                    elif clicked == "erase":
                        tool  = "erase"
                        state = "erasing"
                    else:
                        h = hit_handle(mx, my)
                        if h:
                            drag_handle = h
                            orig_rect   = pygame.Rect(sel_rect)
                            state       = "resizing"
                        elif sel_rect.collidepoint(mx, my):
                            drag_offset = (mx - sel_rect.x, my - sel_rect.y)
                            orig_rect   = pygame.Rect(sel_rect)
                            state       = "moving"
                        else:
                            # Click outside - start a fresh selection
                            sel_start = (mx, my)
                            sel_rect  = None
                            tool      = None
                            strokes   = []
                            draw_surf.fill((0, 0, 0, 0))
                            state     = "selecting"

                elif state in ("drawing", "erasing"):
                    # Toolbar stays fully clickable even while annotating.
                    # Without this check the toolbar clicks were swallowed and
                    # the user had to press Esc before switching tools.
                    btns, _ = toolbar_rects(sel_rect)
                    clicked = next((a for l, a, r in btns if r.collidepoint(mx, my)), None)

                    if   clicked == "cancel": return None, None
                    elif clicked == "save":   return "save",   (pil_full, draw_surf, sel_rect)
                    elif clicked == "saveas": return "saveas", (pil_full, draw_surf, sel_rect)
                    elif clicked == "copy":   return "copy",   (pil_full, draw_surf, sel_rect)
                    elif clicked == "undo":
                        if strokes:
                            strokes.pop()
                            redraw_strokes()
                    elif clicked == "draw":
                        tool  = "draw"
                        state = "drawing"
                    elif clicked == "erase":
                        tool  = "erase"
                        state = "erasing"
                    else:
                        # Regular canvas click - begin a new stroke
                        cur_pts = [(mx, my)]
                        if state == "erasing":
                            erase_circle(draw_surf, mx, my, ERASE_R)

            elif ev.type == pygame.MOUSEMOTION:
                pressed = pygame.mouse.get_pressed()[0]
                if state == "selecting" and sel_start and pressed:
                    sel_rect = sel_from(sel_start, (mx, my))
                elif state == "resizing" and pressed:
                    sel_rect = apply_handle_drag(drag_handle, orig_rect, mx, my, 0, 0)
                elif state == "moving" and pressed:
                    nx = mx - drag_offset[0]
                    ny = my - drag_offset[1]
                    sel_rect = pygame.Rect(
                        max(0, min(nx, W - sel_rect.w)),
                        max(0, min(ny, H - sel_rect.h)),
                        sel_rect.w, sel_rect.h)
                elif state == "drawing" and cur_pts and pressed:
                    prev = cur_pts[-1]
                    cur_pts.append((mx, my))
                    pygame.draw.line(draw_surf, (*draw_color, 255), prev, (mx, my), DRAW_W)
                elif state == "erasing" and pressed:
                    cur_pts.append((mx, my))
                    erase_circle(draw_surf, mx, my, ERASE_R)

            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                if state == "selecting" and sel_start:
                    sel_rect = sel_from(sel_start, (mx, my))
                    if sel_rect.width > 5 and sel_rect.height > 5:
                        state = "selected"
                    else:
                        sel_rect = sel_start = None   # too small, ignore
                elif state == "resizing":
                    drag_handle = None
                    state       = "selected"
                elif state == "moving":
                    drag_offset = None
                    state       = "selected"
                elif state in ("drawing", "erasing"):
                    if len(cur_pts) >= 1:
                        mode = "draw" if state == "drawing" else "erase"
                        strokes.append((mode, draw_color, list(cur_pts)))
                    cur_pts = []
                    # Stay in draw/erase - no state change on mouse-up

        # -- Render ------------------------------------------------------------

        # Base: dimmed full-screen screenshot
        screen.blit(surf_dim, (0, 0))

        # Inside the selection: show the original undimmed screenshot
        if sel_rect and sel_rect.width > 2 and sel_rect.height > 2:
            screen.blit(surf_full, sel_rect.topleft, sel_rect)

        # Draw annotations on top (transparent outside drawn areas)
        if sel_rect:
            screen.blit(draw_surf, (0, 0))

        # Selection border
        if sel_rect and sel_rect.width > 2:
            pygame.draw.rect(screen, COL_SEL_EDGE, sel_rect, 2)

            # Show resize handles only when not in annotation mode
            if state in ("selected", "resizing", "moving"):
                for name, hr in handle_rects(sel_rect).items():
                    pygame.draw.rect(screen, COL_HANDLE,    hr)
                    pygame.draw.rect(screen, COL_HANDLE_BD, hr, 1)

        # Eraser: draw a circle following the cursor so the user can see its size
        if state == "erasing":
            pygame.draw.circle(screen, (255, 255, 255), (mx, my), ERASE_R, 2)

        # Toolbar (visible whenever there's an active selection)
        if state in ("selected", "resizing", "moving", "drawing", "erasing") and sel_rect:
            btns, tb_y = toolbar_rects(sel_rect)
            total_w    = btns[-1][2].right - btns[0][2].left + 4
            tb_surf    = pygame.Surface((total_w, TB_H), pygame.SRCALPHA)
            tb_surf.fill((*COL_TB_BG, 230))
            screen.blit(tb_surf, (btns[0][2].left - 2, tb_y - 4))

            for label, action, r in btns:
                is_hover  = r.collidepoint(mx, my)
                is_active = (action == "draw"  and tool == "draw") \
                         or (action == "erase" and tool == "erase")

                if action == "X":
                    bg_col  = (180, 40, 40) if is_hover else (140, 30, 30)
                    txt_col = (255, 255, 255)
                elif is_active:
                    bg_col  = COL_ACTIVE
                    txt_col = (255, 255, 255)
                elif is_hover:
                    bg_col  = COL_HOVER
                    txt_col = COL_TB_TEXT
                else:
                    bg_col  = COL_TB_BG
                    txt_col = COL_TB_TEXT

                pygame.draw.rect(screen, bg_col, r, border_radius=3)
                txt_surf = font.render(label, True, txt_col)
                screen.blit(txt_surf, (r.x + BTN_PAD, r.y + (BTN_H - txt_surf.get_height()) // 2))

        # Hint text before the user has made a selection
        if state == "selecting" and not sel_start:
            hint = font.render("Click and drag to select  •  Esc to cancel", True, (255, 255, 255))
            screen.blit(hint, ((W - hint.get_width()) // 2, 24))

        pygame.display.flip()

    return None, None


# -- Compose final image from selection + annotations --------------------------
def crop_and_compose(pil_full, draw_surf, sel_rect):
    """
    Crop the selected region from the original screenshot and alpha-composite
    any annotations drawn on top of it. Returns a plain RGB PIL Image.
    """
    import pygame
    from PIL import Image

    x, y, w, h = sel_rect.x, sel_rect.y, sel_rect.width, sel_rect.height
    cropped    = pil_full.crop((x, y, x + w, y + h))

    # Extract just the annotation layer over the selected area
    draw_crop  = pygame.Surface((w, h), pygame.SRCALPHA)
    draw_crop.blit(draw_surf, (0, 0), pygame.Rect(x, y, w, h))
    draw_pil   = Image.frombytes("RGBA", (w, h), pygame.image.tostring(draw_crop, "RGBA"))

    return Image.alpha_composite(cropped.convert("RGBA"), draw_pil).convert("RGB")


# -- Entry point ---------------------------------------------------------------
def main():
    os.makedirs(DEFAULT_DIR, exist_ok=True)

    try:
        screen_png = grab_screen()
    except RuntimeError as e:
        print(f"Screen capture failed: {e}")
        sys.exit(1)

    action, data = run_ui(screen_png)

    import pygame
    pygame.quit()
    os.remove(screen_png)   # clean up portal temp file

    if action is None or data is None:
        sys.exit(0)

    pil_full, draw_surf, sel_rect = data
    final_img = crop_and_compose(pil_full, draw_surf, sel_rect)

    # Write to a temp file first so we have something to work with regardless
    # of whether the user wants to copy, save, or save-as
    tmp = tempfile.mktemp(suffix=".png")
    final_img.save(tmp)

    if action == "copy":
        ok = copy_to_clipboard(tmp)
        os.remove(tmp)
        notify("Snapick", "Copied to clipboard" if ok else "Copy failed")

    elif action == "save":
        dest = next_filename(get_save_dir())
        shutil.copy2(tmp, dest)
        os.remove(tmp)
        notify("Snapick", f"Saved: {os.path.basename(dest)}")

    elif action == "saveas":
        chosen = pick_savepath()
        if chosen:
            set_save_dir(os.path.dirname(chosen))   # remember dir for next time
            shutil.copy2(tmp, chosen)
            notify("Snapick", f"Saved: {os.path.basename(chosen)}")
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    main()
