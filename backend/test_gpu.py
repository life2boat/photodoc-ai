import torch

print("=== Проверка AI-движка ===")
print(f"Версия PyTorch: {torch.__version__}")
print(f"CUDA доступна: {torch.cuda.is_available()}")

if torch.cuda.is_available():
    print(f"Видеокарта: {torch.cuda.get_device_name(0)}")
    print(f"Доступная память: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
else:
    print("🚨 Внимание: видеокарта не найдена, вычисления будут идти на процессоре (очень медленно).")