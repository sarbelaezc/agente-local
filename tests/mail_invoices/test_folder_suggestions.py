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


def test_invoice_content_overrides_sender_rules_and_history():
    import json

    class InspectingModel(FakeModel):
        def complete_json(self, system, user):
            payload = json.loads(user)
            assert payload["correo_nuevo"]["cuerpo"] == "Consulta hospitalaria"
            assert payload["correo_nuevo"]["facturas"][0]["conceptos"] == "Consulta general"
            assert payload["correo_nuevo"]["facturas"][1]["proveedor"] == "Otra clinica"
            return super().complete_json(system, user)

    model = InspectingModel({"folder": "Personal", "reason": "Servicios de salud"})
    result = suggest_folder(
        IncomingMessage(
            "facturas@energia.example", "Factura", body="Consulta hospitalaria",
            invoices=(
                {"supplier": "Hospital", "descriptions": "Consulta general"},
                {"supplier": "Otra clinica", "evidence": "Laboratorio"},
            ),
        ),
        FOLDERS, HISTORY, {"@energia.example": "Facturas/Energía"}, model,
    )
    assert result.folder == "Personal"
    assert result.source == "model"
    assert model.calls == 1


def test_content_without_model_does_not_fall_back_to_sender():
    result = suggest_folder(
        IncomingMessage("facturas@energia.example", "Factura", body="Consulta medica"),
        FOLDERS, HISTORY, {"@energia.example": "Facturas/Energía"},
    )
    assert result.folder is None
