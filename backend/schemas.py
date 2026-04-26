from pydantic import BaseModel
from typing import List
from datetime import datetime

class OrderFileBase(BaseModel):
    id: int
    file_name: str
    file_path: str

    class Config:
        from_attributes = True

class OrderBase(BaseModel):
    user_name: str
    user_phone: str
    format: str
    paper_type: str
    crop_mode: str
    total_price: float

class OrderResponse(OrderBase):
    id: int
    created_at: datetime
    files: List[OrderFileBase] = []

    class Config:
        from_attributes = True