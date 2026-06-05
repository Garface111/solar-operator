"""
Canonical reference examples for hierarchical spreadsheet extraction.

Each example represents a full nested extraction from a real-world roster shape:
    name (operator) → clients → logins → accounts → arrays

These are serialized into LLM extraction prompts as few-shot examples so the
model produces the exact nested output shape for any spreadsheet layout.
"""
from __future__ import annotations

# 1. GMCS-style — community solar operator, 2 clients, mixed NEPOOL IDs,
#    one client has multiple accounts.
EXAMPLE_GMCS_STYLE: dict = {
    "name": "Vermont Community Solar Agents LLC",
    "clients": [
        {
            "name": "Tannery Brook Farm",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "tbfarm@example.com",
                    "accounts": [
                        {
                            "account_number": "4123456",
                            "arrays": [
                                {
                                    "name": "Tannery Brook Array A",
                                    "nepool_gis_id": "53984",
                                    "notes": None,
                                },
                                {
                                    "name": "Tannery Brook Array B",
                                    "nepool_gis_id": "53985",
                                    "notes": "South-facing",
                                },
                            ],
                        },
                        {
                            "account_number": "4123457",
                            "arrays": [
                                {
                                    "name": "Tannery Brook Array C",
                                    "nepool_gis_id": "53986",
                                    "notes": None,
                                },
                            ],
                        },
                    ],
                }
            ],
        },
        {
            "name": "Starlake Holdings",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "starlake@example.com",
                    "accounts": [
                        {
                            "account_number": "4987001",
                            "arrays": [
                                {
                                    "name": "Starlake North",
                                    "nepool_gis_id": "61200",
                                    "notes": None,
                                },
                                {
                                    "name": "Starlake South",
                                    "nepool_gis_id": "61201",
                                    "notes": None,
                                },
                            ],
                        },
                        {
                            "account_number": "4987002",
                            "arrays": [
                                {
                                    "name": "Starlake East",
                                    "nepool_gis_id": "61202",
                                    "notes": "Phase 2",
                                },
                            ],
                        },
                        {
                            "account_number": "4987003",
                            "arrays": [
                                {
                                    "name": "Starlake West",
                                    "nepool_gis_id": None,
                                    "notes": "NEPOOL pending",
                                },
                            ],
                        },
                    ],
                }
            ],
        },
    ],
}

# 2. Residential portfolio — 1 operator, 4 separate residential clients,
#    each with their own GMP login and 1-2 arrays.
EXAMPLE_RESIDENTIAL_PORTFOLIO: dict = {
    "name": "Green Valley Solar Services",
    "clients": [
        {
            "name": "Alice Moreau",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "alice.moreau@example.com",
                    "accounts": [
                        {
                            "account_number": "5001001",
                            "arrays": [
                                {
                                    "name": "Moreau Residence",
                                    "nepool_gis_id": "44100",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "name": "Robert Chagnon",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "rchagnon@example.com",
                    "accounts": [
                        {
                            "account_number": "5001002",
                            "arrays": [
                                {
                                    "name": "Chagnon Home",
                                    "nepool_gis_id": "44101",
                                    "notes": None,
                                },
                                {
                                    "name": "Chagnon Barn",
                                    "nepool_gis_id": "44102",
                                    "notes": "Outbuilding",
                                },
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "name": "Linda St. Pierre",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "lstpierre@example.com",
                    "accounts": [
                        {
                            "account_number": "5001003",
                            "arrays": [
                                {
                                    "name": "St. Pierre Solar",
                                    "nepool_gis_id": "44103",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "name": "Marco Pelletier",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "mpelletier@example.com",
                    "accounts": [
                        {
                            "account_number": "5001004",
                            "arrays": [
                                {
                                    "name": "Pelletier Farm North",
                                    "nepool_gis_id": "44104",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    ],
}

# 3. Mixed VEC+GMP — 1 operator, 2 clients, one client has both a GMP login
#    and a VEC login.
EXAMPLE_MIXED_VEC_GMP: dict = {
    "name": "Northeast Renewable Consultants",
    "clients": [
        {
            "name": "Champlain Valley Organics",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "cvo-gmp@example.com",
                    "accounts": [
                        {
                            "account_number": "6100001",
                            "arrays": [
                                {
                                    "name": "CVO Main Array",
                                    "nepool_gis_id": "72300",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                },
                {
                    "utility": "vec",
                    "login_email": "cvo-vec@example.com",
                    "accounts": [
                        {
                            "account_number": "VEC-88801",
                            "arrays": [
                                {
                                    "name": "CVO East Field",
                                    "nepool_gis_id": "72301",
                                    "notes": "VEC metered",
                                },
                            ],
                        }
                    ],
                },
            ],
        },
        {
            "name": "Hardwick Solar Coop",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "hardwick@example.com",
                    "accounts": [
                        {
                            "account_number": "6100010",
                            "arrays": [
                                {
                                    "name": "Hardwick Array 1",
                                    "nepool_gis_id": "72310",
                                    "notes": None,
                                },
                                {
                                    "name": "Hardwick Array 2",
                                    "nepool_gis_id": "72311",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    ],
}

# 4. Sparse / messy — 3 clients, several arrays missing NEPOOL IDs, one
#    client that has 3 logins (spouse + business + personal), one array with
#    no account number.
EXAMPLE_SPARSE_MESSY: dict = {
    "name": "Northshire Energy Advisors",
    "clients": [
        {
            "name": "Benoit Family Trust",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "benoit.personal@example.com",
                    "accounts": [
                        {
                            "account_number": "7200001",
                            "arrays": [
                                {
                                    "name": "Benoit Farm",
                                    "nepool_gis_id": None,
                                    "notes": "NEPOOL not yet assigned",
                                },
                            ],
                        }
                    ],
                },
                {
                    "utility": "gmp",
                    "login_email": "benoit.spouse@example.com",
                    "accounts": [
                        {
                            "account_number": "7200002",
                            "arrays": [
                                {
                                    "name": "Benoit Cottage",
                                    "nepool_gis_id": "81450",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                },
                {
                    "utility": "gmp",
                    "login_email": "benoitllc@example.com",
                    "accounts": [
                        {
                            "account_number": None,
                            "arrays": [
                                {
                                    "name": "Benoit LLC Solar",
                                    "nepool_gis_id": None,
                                    "notes": "Account # TBD",
                                },
                            ],
                        }
                    ],
                },
            ],
        },
        {
            "name": "Rutland Solar Partners",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "rutlandsolar@example.com",
                    "accounts": [
                        {
                            "account_number": "7200010",
                            "arrays": [
                                {
                                    "name": "Rutland Array Alpha",
                                    "nepool_gis_id": "81460",
                                    "notes": None,
                                },
                                {
                                    "name": "Rutland Array Beta",
                                    "nepool_gis_id": None,
                                    "notes": "Meter swapped Q3",
                                },
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "name": "Johnson Hill Energy",
            "logins": [
                {
                    "utility": "gmp",
                    "login_email": "johnsonhill@example.com",
                    "accounts": [
                        {
                            "account_number": "7200020",
                            "arrays": [
                                {
                                    "name": "Johnson Hill Main",
                                    "nepool_gis_id": "81470",
                                    "notes": None,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    ],
}

ALL_EXAMPLES: list[dict] = [
    EXAMPLE_GMCS_STYLE,
    EXAMPLE_RESIDENTIAL_PORTFOLIO,
    EXAMPLE_MIXED_VEC_GMP,
    EXAMPLE_SPARSE_MESSY,
]
