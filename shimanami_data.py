"""Static reference data for the Shimanami rental-cycle booking API.

 Port ids and cycle-type strings are taken verbatim from the live endpoint:
    https://shimanami.sports.navitime.jp/shimanami/bookings/stocks
"""

# port id -> (English label, short Japanese label). Order = display order.
TERMINALS = [
    ("806821", "Onomichi Station",       "①尾道駅前"),
    ("806833", "Mukaishima",             "②向島"),
    ("806834", "Innoshima",              "③因島"),
    ("806835", "Setoda Tourist Info",    "④瀬戸田観光案内所"),
    ("806836", "Setoda Sunset Beach",    "⑤サンセット"),
    ("806838", "Omishima (Tatara Park)", "⑥大三島"),
    ("806839", "Hakatajima",             "⑦伯方島"),
    ("806841", "Oshima",                 "⑧大島"),
    ("806842", "Sunrise Itoyama",        "⑨糸山"),
    ("806843", "Imabari Station",        "⑩今治駅前"),
]

TERMINAL_LABEL = {pid: f"{en} ({jp})" for pid, en, jp in TERMINALS}

# API cycle-type string -> English label. Matched EXACTLY, so the child-seat
# e-assist is a separate entry and never counts as the plain e-assist bike.
CYCLE_TYPES = [
    ("電動アシスト自転車",              "Battery-Assisted Bicycle"),
    ("E-bike",                          "E-bike"),
    ("チャイルドシート付電動アシスト自転車", "E-Assist with Child Seat"),
    ("クロスバイク",                    "Cross Bike"),
    ("シティサイクル",                  "City Cycle"),
    ("キッズバイク",                    "Kids Bike"),
    ("タンデム自転車",                  "Tandem"),
]

CYCLE_LABEL = {jp: en for jp, en in CYCLE_TYPES}
