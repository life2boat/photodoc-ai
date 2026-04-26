import requests
import json

BASE_URL = "http://127.0.0.1:8000/api/chat"

print("🚀 Тестируем бота PhotoDoc AI...\n")

# --- ТЕСТ 1: Правильный запрос (Новый контракт) ---
print("Тест 1: 'Привет, сколько стоит фото на паспорт?' (Правильный формат)")
payload_valid = {
    "messages": [
        {"role": "user", "content": "Привет, сколько стоит фото на паспорт?"}
    ]
}

try:
    response = requests.post(BASE_URL, json=payload_valid)
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        if "reply" in data:
            print(f"✅ OK: Бот ответил: {data['reply']}\n")
        else:
            print("❌ ОШИБКА: Сервер вернул 200, но в ответе нет поля 'reply'!\n")
    else:
        print(f"❌ ОШИБКА: Ожидали 200, получили {response.status_code}. Ответ: {response.text}\n")
except Exception as e:
    print(f"❌ СБОЙ СЕТИ: Сервер выключен или недоступен. Ошибка: {e}\n")


# --- ТЕСТ 2: Сломанный запрос (Старый контракт, ожидаем 422) ---
print("Тест 2: 'Сломанный запрос' (Проверка фейс-контроля FastAPI)")
payload_invalid = {
    "message": "Этот запрос должен быть отклонен" # Специально шлем неправильное поле
}

try:
    response = requests.post(BASE_URL, json=payload_invalid)
    print(f"Status: {response.status_code}")
    
    if response.status_code == 422:
        print("✅ OK: Сервер успешно заблокировал кривой запрос (ожидали ошибку валидации)\n")
    else:
        print(f"❌ ОШИБКА: Ожидали 422, а получили {response.status_code}.\n")
except Exception as e:
    print(f"❌ СБОЙ СЕТИ: {e}\n")

print("🎉 ТЕСТИРОВАНИЕ ЗАВЕРШЕНО!")