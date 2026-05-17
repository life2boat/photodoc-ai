import React, { useState } from 'react';
import { ArrowRight, Calculator, Hash, Layers3 } from 'lucide-react';

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
    'Глубокая реставрация': 350,
    'Окрашивание (Колоризация)': 150,
  },
  'Печать и копии': {
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
  },
};

export default function ServiceCalculator() {
  const [category, setCategory] = useState('Фотопечать');
  const [service, setService] = useState(Object.keys(priceList['Фотопечать'])[0]);
  const [quantity, setQuantity] = useState(1);

  const handleCategoryChange = (event) => {
    const nextCategory = event.target.value;
    setCategory(nextCategory);
    setService(Object.keys(priceList[nextCategory])[0]);
  };

  const total = priceList[category][service] * quantity;

  const handleGoToOrder = () => {
    document.getElementById('services-section')?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  };

  const fieldClass = 'w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-yellow-500/50 focus:ring-2 focus:ring-yellow-500/30';

  return (
    <div className="relative mx-auto max-w-4xl overflow-hidden rounded-3xl border border-white/10 bg-zinc-900/50 p-5 shadow-2xl shadow-black/40 backdrop-blur-xl md:p-8">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_0%,rgba(202,138,4,0.10),transparent_32%),radial-gradient(circle_at_80%_20%,rgba(79,70,229,0.12),transparent_34%)]" />
      <div className="relative grid gap-8 lg:grid-cols-[1.1fr_0.9fr]">
        <div>
          <div className="mb-6 flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10 text-yellow-300">
              <Calculator className="h-5 w-5" aria-hidden="true" />
            </div>
            <div>
              <h2 className="text-2xl font-bold tracking-tight text-white">Умный калькулятор</h2>
              <p className="mt-1 text-sm leading-6 text-zinc-300">Быстро оцените стоимость перед оформлением заказа.</p>
            </div>
          </div>

          <div className="space-y-5">
            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">Категория услуг</label>
              <select value={category} onChange={handleCategoryChange} className={fieldClass}>
                {Object.keys(priceList).map((item) => (
                  <option key={item} value={item}>{item}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">Тип услуги / размер</label>
              <select value={service} onChange={(event) => setService(event.target.value)} className={fieldClass}>
                {Object.keys(priceList[category]).map((item) => (
                  <option key={item} value={item}>{item} — {priceList[category][item]} ₽</option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">Количество</label>
              <div className="relative">
                <Hash className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" aria-hidden="true" />
                <input
                  type="number"
                  min="1"
                  value={quantity}
                  onChange={(event) => setQuantity(Math.max(1, parseInt(event.target.value, 10) || 1))}
                  className={`${fieldClass} pl-11`}
                />
              </div>
            </div>
          </div>
        </div>

        <div className="flex flex-col justify-between rounded-3xl border border-white/10 bg-black/25 p-5">
          <div>
            <div className="mb-5 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-violet-300/20 bg-violet-400/10 text-violet-200">
                <Layers3 className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <p className="text-sm font-semibold text-white">Предварительная оценка</p>
                <p className="text-xs text-zinc-400">Итог уточняется после проверки файлов</p>
              </div>
            </div>

            <div className="rounded-2xl border border-yellow-500/15 bg-yellow-500/10 p-5 text-center">
              <p className="text-sm text-zinc-300">Итоговая стоимость</p>
              <p className="mt-2 text-4xl font-bold tracking-tight text-yellow-300">{total} ₽</p>
              <p className="mt-2 text-xs text-zinc-400">{quantity} шт. × {priceList[category][service]} ₽</p>
            </div>
          </div>

          <button
            type="button"
            onClick={handleGoToOrder}
            className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-2xl bg-yellow-400 px-5 py-3 font-bold text-black shadow-[0_0_32px_rgba(202,138,4,0.20)] transition-all hover:scale-[1.01] hover:bg-yellow-300"
          >
            Перейти к заказу
            <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  );
}
