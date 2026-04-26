import React, { useState } from 'react';

const priceList = {
  'Фотопечать': {
    '9x13 см': 20,
    '10x15 см (A6)': 20,
    '13x18 см': 40,
    '15x20 см': 40,
    '21x30 см (A4)': 70,
    '30x40 см': 150,
    '21x30 см (A4) Дипломы': 50,
  },
  'Фото на документы': {
    '3x4 см (2 шт)': 300,
    '3x4 см (4 шт)': 350,
    '3x4 см (6 шт)': 400,
    '3x4 см (8 шт)': 450,
    '3x4 см (10 шт)': 500,
    '3.5x4.5 см Паспорт (2 шт)': 300,
    '3.5x4.5 см Паспорт (4 шт)': 350,
    '4x6 см Военный (2 шт)': 300,
    '9x12 см Личное дело (1 шт)': 300,
  },
  'Печать и Копии': {
    'Печать A4 ч/б (1 лист)': 10,
    'Печать A4 цвет (1 лист)': 25,
    'Печать A3 ч/б (1 лист)': 30,
    'Печать A3 цвет (1 лист)': 60,
    'Ксерокопия A4 ч/б': 10,
    'Сканирование документа': 15,
    'Сканирование фото': 20,
  },
  'Ламинирование': {
    'A4 (210x297 мм)': 50,
    'A3 (297x420 мм)': 90,
    'A5 (148x210 мм)': 40,
    'A6 (105x148 мм)': 30,
    'Визитка / Пропуск': 20,
  },
  'Оцифровка': {
    'Оцифровка видео (1 час)': 480,
    'Перевод видео в DVD (1 час)': 480,
  }
};

export default function ServiceCalculator() {
  const [category, setCategory] = useState('Фотопечать');
  const [service, setService] = useState(Object.keys(priceList['Фотопечать'])[0]);
  const [quantity, setQuantity] = useState(1);

  const handleCategoryChange = (e) => {
    const newCat = e.target.value;
    setCategory(newCat);
    setService(Object.keys(priceList[newCat])[0]);
  };

  const total = priceList[category][service] * quantity;

  return (
    <div style={{ maxWidth: '500px', margin: '3rem auto', padding: '2rem', background: '#fff', borderRadius: '16px', boxShadow: '0 10px 25px rgba(0,0,0,0.1)', fontFamily: 'sans-serif' }}>
      <h2 style={{ textAlign: 'center', color: '#1a1a1a', marginBottom: '1.5rem' }}>Умный калькулятор</h2>
      
      <div style={{ marginBottom: '1rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Категория услуг</label>
        <select value={category} onChange={handleCategoryChange} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd' }}>
          {Object.keys(priceList).map(cat => <option key={cat} value={cat}>{cat}</option>)}
        </select>
      </div>

      <div style={{ marginBottom: '1rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Тип услуги / Размер</label>
        <select value={service} onChange={(e) => setService(e.target.value)} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd' }}>
          {Object.keys(priceList[category]).map(ser => <option key={ser} value={ser}>{ser} — {priceList[category][ser]}₽</option>)}
        </select>
      </div>

      <div style={{ marginBottom: '1.5rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Количество</label>
        <input type="number" min="1" value={quantity} onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd' }} />
      </div>

      <div style={{ padding: '1.5rem', background: '#f8f9fa', borderRadius: '12px', textAlign: 'center' }}>
        <div style={{ fontSize: '14px', color: '#888', marginBottom: '5px' }}>Итоговая стоимость</div>
        <div style={{ fontSize: '32px', fontWeight: 'bold', color: '#2563eb' }}>{total} ₽</div>
      </div>
    </div>
  );
}