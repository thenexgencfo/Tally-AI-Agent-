from app.masking.recognizers import _D, _P, entities_in, find_all, verhoeff_valid

_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_complete(base: str) -> str:
    c = 0
    for i, d in enumerate(reversed(base)):
        c = _D[c][_P[(i + 1) % 8][int(d)]]
    return base + str(_INV[c])


VALID_AADHAAR = verhoeff_complete("23412341234")


def test_pan_detected():
    assert [m.entity for m in find_all("PAN is AABCA1234K.")] == ["PAN"]


def test_gstin_is_one_entity_not_split_into_pan():
    ms = find_all("GSTIN 27AABCA1234K1Z5 registered")
    assert [(m.entity, m.value) for m in ms] == [("GSTIN", "27AABCA1234K1Z5")]


def test_ifsc_email_phone():
    ents = entities_in("IFSC HDFC0001234, mail a.b@acme.example, call +91 98200 11223 or 9876543210")
    assert ents == {"IFSC", "EMAIL", "PHONE"}


def test_aadhaar_requires_valid_checksum():
    assert verhoeff_valid(VALID_AADHAAR)
    assert entities_in(f"Aadhaar {VALID_AADHAAR}") == {"AADHAAR"}
    spaced = f"{VALID_AADHAAR[:4]} {VALID_AADHAAR[4:8]} {VALID_AADHAAR[8:]}"
    assert entities_in(f"Aadhaar {spaced}") == {"AADHAAR"}
    bad = VALID_AADHAAR[:-1] + str((int(VALID_AADHAAR[-1]) + 1) % 10)
    assert "AADHAAR" not in entities_in(f"Aadhaar {bad}")


def test_amounts_and_invoice_numbers_are_not_masked():
    text = "Invoice SI-001 for 150000.00 dated 2024-04-10, total 10,51,000.00, ref 202404100001"
    assert find_all(text) == []


def test_account_numbers_masked_by_context_or_column():
    assert entities_in("acct 50200012345678") == {"ACCOUNT"}          # keyword context
    assert find_all("reference 50200012345678") == []                 # no context: not assumed
    assert entities_in("50200012345678", include_column_only=True) == {"ACCOUNT"}


def test_lowercase_identifiers_are_matched():
    # People type identifiers in lowercase; they must still be masked.
    assert entities_in("aabca1234k") == {"PAN"}
    assert entities_in("27aabca1234k1z5") == {"GSTIN"}
