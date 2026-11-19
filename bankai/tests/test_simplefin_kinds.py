from bankai.connectors.simplefin import _guess_kind


def test_visa_and_amex_are_credit():
    assert _guess_kind("Customized Cash Rewards Visa Signature- 4771") == "credit"
    assert _guess_kind("Travel Rewards Visa Signature- 5970") == "credit"
    assert _guess_kind("Amex Blue Cash Everyday") == "credit"
    assert _guess_kind("BankAmericard Platinum Plus Mastercard- 1420") == "credit"


def test_institution_fallback_for_unhinted_names():
    assert _guess_kind("Individual (5081)", "Fidelity Investments Trust") == "investment"
    assert _guess_kind("Trust: Under Agreement (9880)", "Fidelity Investments Trust") == "investment"


def test_name_hint_beats_institution():
    assert _guess_kind("Advantage Savings- 9536", "Fidelity Investments Trust") == "savings"


def test_plain_accounts_stay_checking():
    assert _guess_kind("Adv Plus Banking- 2724", "Bank of America") == "checking"
    assert _guess_kind("Everyday account") == "checking"
