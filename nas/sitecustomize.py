# NAS (ds1821): rejestracja modelu embeddera spoza listy FastEmbed - ten sam co w patronie
# (Xenova/multilingual-e5-small, 384d, mean pooling + L2, prefiksy e5 "query: "/"passage: ").
# Ladowany automatycznie przez Pythona (PYTHONPATH=/app/nas w docker-compose.nas.yml) - bez zmian w kodzie upstreamu.
try:
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    TextEmbedding.add_custom_model(
        model="intfloat/multilingual-e5-small",
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf="Xenova/multilingual-e5-small"),
        dim=384,
        model_file="onnx/model.onnx",
    )
except Exception:  # brak fastembed albo model juz zarejestrowany - nie blokuj startu
    pass
