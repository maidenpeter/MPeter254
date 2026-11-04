from cargoshield import awb


def test_check_digit_is_first_seven_mod_seven():
    assert awb.check_digit("1234567") == 1234567 % 7 == 5
    assert awb.check_digit("0000000") == 0
    assert awb.make("999", "1234567") == "99912345675"


def test_validate_good_bad_and_malformed():
    assert awb.validate("999-12345675").status == "ok"
    assert awb.validate("999 1234 5675").status == "ok"
    assert awb.validate("99912345675").awb == "99912345675"
    assert awb.validate("999-12345676").status == "bad_check_digit"
    assert awb.validate("999-1234567").status == "bad_format"
    assert awb.validate("hello").status == "bad_format"


def test_make_invalid_always_fails_the_check():
    for serial in ("1234567", "0000000", "6666666", "4718203", "1000006"):
        bad = awb.make_invalid("999", serial)
        assert awb.validate(bad).status == "bad_check_digit"


def test_find_in_text_handles_formats_and_ocr_slips():
    assert awb.find_in_text("Please check 999-12345675 thanks") == ["99912345675"]
    assert awb.find_in_text("AWB 999 1234 5675") == ["99912345675"]
    # O for 0 and l for 1, typical OCR slips
    assert "99912345675" in awb.find_in_text("999-l2345675", fix_ocr=True)
    assert "99910345675" in awb.find_in_text("999-1O345675", fix_ocr=True)
    assert awb.find_in_text("no numbers here") == []


def test_fmt():
    assert awb.fmt("99912345675") == "999-12345675"
