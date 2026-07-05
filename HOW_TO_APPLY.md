# Amenities categories update — how to apply

## What changed
1. `app.py`
   - Added a `FEATURE_CATALOG` (6 categories: Amenities, Entertainment, Internet, Kitchen, Pets, Suitability — each with a fixed list of selectable tags, EN + LV labels).
   - Added a `features` column to the `Room` model (stores selected tags as JSON).
   - Added `save_room_features()` helper, wired into the add/edit room routes.
2. `templates/admin/room_form.html` — room add/edit form now shows checkboxes grouped by category (chip style), instead of a single amenities text box.
3. `templates/room_detail.html` — the room page now renders **all 6 tabs dynamically** from the room's saved tags (previously Internet/Kitchen/Suitability were hardcoded and identical for every room).
4. `static/css/style.css` — added `.feature-chip-toggle` / `.feature-cat` styles for the new checkbox picker.
5. `add_features_column.py` — one-time migration script (only needed if your app **doesn't** already auto-migrate; see note below).

## Steps
1. Copy these 5 files into your repo, overwriting the existing ones at the same paths:
   - `app.py`
   - `templates/admin/room_form.html`
   - `templates/room_detail.html`
   - `static/css/style.css`
   - `add_features_column.py` (new file, project root)

2. Your `app.py` already has an `auto_migrate()` function that runs on startup and
   auto-adds any new model columns to the SQLite database — so the `features`
   column will be added automatically the next time the app starts. You do NOT
   need to run `add_features_column.py` manually unless you see an error like
   `no such column: room.features`. If that happens, run:
   ```
   python add_features_column.py
   ```

3. Restart the app. Existing rooms will show empty categories (that's expected —
   go into each room's Edit page and tick the boxes that apply). The old
   "Amenities highlight" text field still works exactly as before and still
   feeds the small tags shown on room cards on the homepage/listing page.

4. Test:
   - Go to `/admin/rooms/new` or edit an existing room — you'll see 6 grouped
     checkbox sections (Amenities, Entertainment, Internet, Kitchen, Pets, Suitability).
   - Tick a few boxes, save.
   - Open that room's public page — the matching tabs will now show ✓ for the
     ticked items, and "No information added" for categories with nothing ticked.

## Notes
- Tags are stored as JSON like:
  `{"amenities": ["heating", "parking"], "pets": ["pets_allowed"]}`
- To add/remove available tag options later, edit `FEATURE_CATALOG` in `app.py` —
  the form and room page pick up new options automatically.
- ⚠️ Reminder from earlier: rotate your Google OAuth client secret and Flask
  `SECRET_KEY` since a `.env` file with real values was briefly committed to the
  now-public GitHub repo. Then re-set the repo to private if you'd rather keep
  it that way.
