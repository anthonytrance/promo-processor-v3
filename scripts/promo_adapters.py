"""Registry of finished source adapters: platform name, module and class. Imports stay lazy, so promo_mail can read
the names without loading any platform code.

The order is the coordinator's tie-break when two sources offer the same mix in an equal format: platforms that
serve the label's own original come first, general file shares last.
"""
import importlib

ADAPTERS = {
    "inflyte": ("inflyte", "Inflyte"),
    "promo-cloud": ("promo_cloud", "PromoCloud"),
    "promoly": ("promoly", "Promoly"),
    "ampsuite": ("ampsuite", "Ampsuite"),
    "label-engine": ("labelengine", "LabelEngine"),
    "promobox": ("promobox", "PromoBox"),
    "promochoons": ("promochoons", "Promochoons"),
    "disco": ("disco", "Disco"),
    "promo-delivery": ("promodelivery", "PromoDelivery"),
    "promopush": ("promopush", "PromoPush"),
    "beatrolo": ("beatrolo", "BeatRolo"),
    "needlepool": ("needlepool", "Needlepool"),
    "ctrl-promo": ("ctrlpromo", "CtrlPromo"),
    "fatdrop": ("fatdrop", "Fatdrop"),
    "label-portal": ("label_portals", "LabelPortals"),
    "hearthis": ("hearthis", "HearThis"),
    "soundcloud": ("soundcloud", "SoundCloud"),
    "dropbox": ("dropbox_promos", "DropboxPromos"),
    "google-drive": ("gdrive_promos", "GoogleDrivePromos"),
    "onedrive": ("onedrive_promos", "OneDrivePromos"),
    "wetransfer": ("wetransfer_promos", "WeTransferPromos"),
}

PLATFORMS = tuple(ADAPTERS)


def adapter_for(platform, store):
    if platform not in ADAPTERS:
        raise ValueError("Unknown adapter")
    module, name = ADAPTERS[platform]
    return getattr(importlib.import_module(module), name)(store)
