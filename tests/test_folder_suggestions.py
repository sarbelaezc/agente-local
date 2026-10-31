from mail_invoices.folders import FiledMessage, IncomingMessage, suggest_folder

FOLDERS = ["Facturas/Energía", "Facturas/Internet", "Personal"]
HISTORY = [
    FiledMessage("facturas@energia.example", "Su factura de enero", "Facturas/Energía"),
    FiledMessage("facturas@energia.example", "Su factura de febrero", "Facturas/Energía"),
    FiledMessage("cobros@internet.example", "Factura mensual", "Facturas/Internet"),
]


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return self.answer


def test_confirmed_rule_wins_without_calling_model():
    model = FakeModel({"folder": "Personal"})
    result = suggest_folder(
        IncomingMessage("x@internet.example", "Factura"), FOLDERS, HISTORY,
        {"@internet.example": "Facturas/Internet"}, model,
    )
    assert (result.folder, result.source) == ("Facturas/Internet", "rule")
    assert model.calls == 0


def test_rule_pointing_to_missing_folder_is_not_suggested():
    result = suggest_folder(
        IncomingMessage("a@b.example", "Factura"), FOLDERS, (), {"a@b.example": "Nueva"}
    )
    assert result.folder is None


def test_unanimous_history_is_used_before_model():
    model = FakeModel({"folder": "Personal"})
    result = suggest_folder(
        IncomingMessage("Facturas@Energia.example", "Factura marzo"), FOLDERS, HISTORY, None, model
    )
    assert (result.folder, result.source) == ("Facturas/Energía", "history")
    assert model.calls == 0


def test_model_suggestion_must_be_an_existing_folder():
    model = FakeModel({"folder": "Facturas/Agua", "reason": "nueva"})
    result = suggest_folder(IncomingMessage("n@agua.example", "Factura"), FOLDERS, HISTORY, None, model)
    assert result.folder is None
    model = FakeModel({"folder": "Facturas/Internet", "reason": "Proveedor similar"})
    result = suggest_folder(IncomingMessage("n@tv.example", "Factura"), FOLDERS, HISTORY, None, model)
    assert (result.folder, result.source) == ("Facturas/Internet", "model")


def test_without_model_unknown_sender_has_no_suggestion():
    result = suggest_folder(IncomingMessage("n@new.example", "Factura"), FOLDERS, HISTORY)
    assert result.folder is None
