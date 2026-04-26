import os
from dotenv import load_dotenv
load_dotenv()

import shutil
import traceback
import io
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Literal
from fastapi import FastAPI, Depends, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from openai import AsyncOpenAI

from database import Base, engine, get_db
from models import Order, OrderFile

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Инициализация ИИ-клиента
ai_client = AsyncOpenAI(
    api_key=os.getenv("VSEGPT_API_KEY"),
    base_url=os.getenv("VSEGPT_BASE_URL")
)

app = FastAPI(title="PhotoDoc AI - Business Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
Base.metadata.create_all(bind=engine)

class StatusUpdate(BaseModel):
    status: str

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]

class ChatResponse(BaseModel):
    reply: str

# --- ИИ-КОНСУЛЬТАНТ (Чат) ---
@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(payload: ChatRequest):
    try:
        if not payload.messages:
            return JSONResponse(
                status_code=400,
                content={"reply": "История сообщений пуста."}
            )

        # 1. Системный промпт с зашитыми бизнес-правилами
        system_message = {
            "role": "system",
            "content": (
                "Ты вежливый виртуальный консультант фотостудии PhotoDoc. "
                "Твоя цель — помогать клиентам и направлять их к оформлению заказа. "
                "Актуальные цены: Любое фото на документы (3х4, 3.5х4.5, 4х6, 9х12) стоит ровно 300 рублей. "
                "Печать фото: 10х15 - 20 руб, А4 - 70 руб. Реставрация фото рассчитывается индивидуально. "
                "Отвечай кратко, приветливо и только по делу."
            )
        }

        # 2. Формируем итоговый список сообщений: система + история от клиента
        api_messages = [system_message] + [{"role": msg.role, "content": msg.content} for msg in payload.messages]

        # 3. Асинхронный запрос к нейросети
        response = await ai_client.chat.completions.create(
            model="openai/gpt-4o-mini",  # Базовая, быстрая модель VSEGPT
            messages=api_messages,
            max_tokens=300,
            temperature=0.7
        )
        
        # 4. Извлекаем текст ответа и возвращаем строго по контракту
        reply_text = response.choices[0].message.content
        return ChatResponse(reply=reply_text)

    except Exception as e:
        # Логируем детальную ошибку в терминал сервера для администратора
        print("!!! ОШИБКА ИНТЕГРАЦИИ VSEGPT !!!")
        traceback.print_exc()
        
        # Возвращаем безопасный JSON с текстом ошибки для отображения в UI фронтенда
        return JSONResponse(
            status_code=500, 
            content={"reply": "Простите, у меня возникли временные неполадки. Пожалуйста, оформите заказ напрямую через форму!"}
        )

# --- ПРИЕМ ЗАКАЗОВ (Печать, Документы, Реставрация, Полароид) ---
@app.post("/api/order")
async def create_order(
    name: str = Form(...),
    phone: str = Form(...),
    comment: str = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    order = Order(
        name=name,
        phone=phone,
        comment=comment,
        source="web_site",
        status="new",
        created_at=datetime.utcnow()
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    
    order_dir = UPLOAD_DIR / str(order.id)
    order_dir.mkdir(parents=True, exist_ok=True)
    
    for f in files:
        file_path = order_dir / f.filename
        with file_path.open("wb") as buffer:
            shutil.copyfileobj(f.file, buffer)
            
        db_file = OrderFile(
            order_id=order.id,
            filename=f.filename,
            path=str(file_path),
            mime_type=f.content_type
        )
        db.add(db_file)
    
    db.commit()
    return {"ok": True, "order_id": order.id}

# --- АДМИН-ПАНЕЛЬ ---
@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request, db: Session = Depends(get_db)):
    orders = db.query(Order).order_by(desc(Order.created_at)).all()
    return templates.TemplateResponse(
        request=request, 
        name="admin.html", 
        context={"request": request, "orders": orders}
    )

@app.get("/admin/orders/{order_id}/download")
def download_zip(order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        for f in order.files:
            p = Path(f.path)
            if p.exists():
                zip_file.write(p, arcname=f.filename)
        
        order_info_text = (
            f"=== ИНФОРМАЦИЯ О ЗАКАЗЕ №{order.id} ===\n"
            f"Дата: {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"Клиент: {order.name}\n"
            f"Телефон: {order.phone}\n"
            f"Детали:\n{order.comment}\n"
            f"===================================\n"
        )
        zip_file.writestr("order_info.txt", order_info_text.encode('utf-8'))
    
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer, 
        media_type="application/x-zip-compressed", 
        headers={"Content-Disposition": f"attachment; filename=Order_{order_id}.zip"}
    )

@app.post("/admin/orders/{order_id}/status")
def update_status(order_id: int, data: StatusUpdate, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        order.status = data.status
        db.commit()
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Order not found")