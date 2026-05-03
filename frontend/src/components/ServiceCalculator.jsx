import React, { useState } from 'react';
import { supabase } from '../lib/supabase';

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
  'Реставрация фото': {
    'Легкая реставрация': 200,
    'Глубокая реставрация с ИИ': 350,
    'Окрашивание (Колоризация)': 150,
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

  // Form states
  const [clientName, setClientName] = useState('');
  const [clientPhone, setClientPhone] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitStatus, setSubmitStatus] = useState(null);

  const handleCategoryChange = (e) => {
    const newCat = e.target.value;
    setCategory(newCat);
    setService(Object.keys(priceList[newCat])[0]);
  };

  const total = priceList[category][service] * quantity;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!clientName || !clientPhone) {
      setSubmitStatus('validation_error');
      return;
    }

    setIsSubmitting(true);
    setSubmitStatus(null);

    try {
      const { error } = await supabase
        .from('orders')
        .insert([
          {
            client_name: clientName,
            client_phone: clientPhone,
            category: category,
            service_name: service,
            quantity: quantity,
            total_price: total,
            status: 'new'
          }
        ]);

      if (error) throw error;

      setSubmitStatus('success');
      setClientName('');
      setClientPhone('');
      setQuantity(1);
    } catch (error) {
      console.error("Error saving order:", error);
      setSubmitStatus('error');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div style={{ maxWidth: '500px', margin: '3rem auto', padding: '2rem', background: '#fff', borderRadius: '16px', boxShadow: '0 10px 25px rgba(0,0,0,0.1)', fontFamily: 'sans-serif' }}>
      <h2 style={{ textAlign: 'center', color: '#1a1a1a', marginBottom: '1.5rem', fontSize: '1.5rem', fontWeight: 'bold' }}>Умный калькулятор</h2>
      
      <div style={{ marginBottom: '1rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Категория услуг</label>
        <select value={category} onChange={handleCategoryChange} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd', color: '#000' }}>
          {Object.keys(priceList).map(cat => <option key={cat} value={cat}>{cat}</option>)}
        </select>
      </div>

      <div style={{ marginBottom: '1rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Тип услуги / Размер</label>
        <select value={service} onChange={(e) => setService(e.target.value)} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd', color: '#000' }}>
          {Object.keys(priceList[category]).map(ser => <option key={ser} value={ser}>{ser} — {priceList[category][ser]}₽</option>)}
        </select>
      </div>

      <div style={{ marginBottom: '1.5rem' }}>
        <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Количество</label>
        <input type="number" min="1" value={quantity} onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))} style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd', color: '#000' }} />
      </div>

      <div style={{ padding: '1.5rem', background: '#f8f9fa', borderRadius: '12px', textAlign: 'center', marginBottom: '1.5rem' }}>
        <div style={{ fontSize: '14px', color: '#888', marginBottom: '5px' }}>Итоговая стоимость</div>
        <div style={{ fontSize: '32px', fontWeight: 'bold', color: '#2563eb' }}>{total} ₽</div>
      </div>

      <hr style={{ border: 'none', borderTop: '1px solid #eee', marginBottom: '1.5rem' }} />

      <h3 style={{ fontSize: '1.1rem', fontWeight: 'bold', color: '#1a1a1a', marginBottom: '1rem' }}>Оформление заказа</h3>

      <form onSubmit={handleSubmit}>
        <div style={{ marginBottom: '1rem' }}>
          <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Ваше имя</label>
          <input type="text" value={clientName} onChange={(e) => setClientName(e.target.value)} placeholder="Иван Иванов" style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd', color: '#000' }} required />
        </div>

        <div style={{ marginBottom: '1.5rem' }}>
          <label style={{ display: 'block', fontSize: '14px', color: '#666', marginBottom: '5px' }}>Телефон для связи</label>
          <input type="tel" value={clientPhone} onChange={(e) => setClientPhone(e.target.value)} placeholder="+7 (999) 000-00-00" style={{ width: '100%', padding: '10px', borderRadius: '8px', border: '1px solid #ddd', color: '#000' }} required />
        </div>

        {submitStatus === 'validation_error' && (
          <div style={{ color: '#dc2626', fontSize: '14px', marginBottom: '1rem', textAlign: 'center' }}>Пожалуйста, заполните имя и телефон.</div>
        )}

        {submitStatus === 'error' && (
          <div style={{ color: '#dc2626', fontSize: '14px', marginBottom: '1rem', textAlign: 'center' }}>Ошибка при отправке заказа. Убедитесь, что таблица настроена верно.</div>
        )}

        {submitStatus === 'success' && (
          <div style={{ color: '#16a34a', fontSize: '14px', marginBottom: '1rem', textAlign: 'center', padding: '10px', background: '#dcfce7', borderRadius: '8px' }}>
            🎉 Заказ успешно оформлен! Мы свяжемся с вами.
          </div>
        )}

        <button
          type="submit"
          disabled={isSubmitting}
          style={{
            width: '100%',
            padding: '12px',
            background: isSubmitting ? '#93c5fd' : '#2563eb',
            color: '#fff',
            border: 'none',
            borderRadius: '8px',
            fontWeight: 'bold',
            fontSize: '16px',
            cursor: isSubmitting ? 'not-allowed' : 'pointer',
            transition: 'background 0.2s'
          }}
        >
          {isSubmitting ? 'Отправка...' : 'Отправить заявку'}
        </button>
      </form>
    </div>
  );
}