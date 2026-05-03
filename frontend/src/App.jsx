import React, { useState } from 'react';
import { Camera, Printer, Wand2, Image as ImageIcon, Star, ShieldCheck, Clock } from 'lucide-react';

// Импортируем все рабочие виджеты
import { PhotoDocWidget } from './components/PhotoDocWidget';
import { PhotoPrintForm } from './components/PhotoPrintForm';
import PolaroidWidget from './components/PolaroidWidget';
import ServiceCalculator from './components/ServiceCalculator'; 
import { RestoreWidget } from './components/RestoreWidget'; 

export default function App() {
  const [activeService, setActiveService] = useState('docs');
  const [isOrderSuccess, setIsOrderSuccess] = useState(false);

  // Услуги для витрины (Реставрация включена)
  const services = [
    { id: 'docs', title: 'Фото на документы', icon: Camera },
    { id: 'print', title: 'Печать фото', icon: Printer },
    { id: 'restore', title: 'Реставрация фото', icon: Wand2 },
    { id: 'polaroid', title: 'Печать Polaroid', icon: ImageIcon },
  ];

  return (
    <div className="min-h-screen bg-black text-white font-sans selection:bg-yellow-400 selection:text-black">
      {/* Шапка */}
      <header className="absolute top-0 w-full py-6 px-6 md:px-12 flex justify-between items-center z-10">
        <div className="text-xl font-extrabold tracking-tight">
          PhotoDoc<span className="text-yellow-400">.</span>
        </div>
        <button onClick={() => document.getElementById('contacts')?.scrollIntoView({ behavior: 'smooth' })} className="text-sm font-medium text-gray-400 hover:text-white transition-colors cursor-pointer">
          Связаться с нами
        </button>
      </header>

      {/* Hero Секция (Главный экран) */}
      <section className="pt-48 pb-32 px-4 md:pt-56 md:pb-40 flex flex-col items-center text-center">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-5xl md:text-7xl font-extrabold tracking-tight text-white mb-8 leading-tight">
            Цифровая фотостудия <br className="hidden md:block" />
            <span className="text-gray-300">нового поколения</span>
          </h1>
          <p className="text-lg md:text-xl text-gray-300 max-w-3xl mx-auto leading-relaxed">
            Профессиональная печать, реставрация и подготовка фото на документы. 
            Безупречное качество, не выходя из дома.
          </p>
          <div className="mt-12 flex flex-col sm:flex-row items-center justify-center gap-4">
            <button onClick={() => document.getElementById('services-section')?.scrollIntoView({ behavior: 'smooth' })} className="px-8 py-4 bg-yellow-400 text-black font-bold rounded-full transition-transform hover:scale-105 w-full sm:w-auto">
              Начать работу
            </button>
            <button onClick={() => document.getElementById('calculator-section')?.scrollIntoView({ behavior: 'smooth' })} className="px-8 py-4 bg-transparent border border-gray-800 text-white font-medium rounded-full transition-colors hover:bg-gray-900 hover:border-gray-700 w-full sm:w-auto cursor-pointer">
              Узнать больше
            </button>
          </div>
        </div>
      </section>

      {/* Блок преимуществ */}
      <section className="py-24 px-4 bg-gray-900/50 border-y border-gray-800">
        <div className="max-w-5xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-16">
          <div className="flex flex-col items-center text-center space-y-5">
            <div className="w-14 h-14 bg-gray-900 rounded-2xl flex items-center justify-center border border-gray-800">
              <Star className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">Ручная проверка каждого макета</h3>
            <p className="text-gray-400 text-sm leading-relaxed">Наши специалисты внимательно оценивают качество перед отправкой в печать.</p>
          </div>
          <div className="flex flex-col items-center text-center space-y-5">
            <div className="w-14 h-14 bg-gray-900 rounded-2xl flex items-center justify-center border border-gray-800">
              <ShieldCheck className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">100% соответствие стандартам</h3>
            <p className="text-gray-400 text-sm leading-relaxed">Идеальные размеры и пропорции для любых видов документов и виз.</p>
          </div>
          <div className="flex flex-col items-center text-center space-y-5">
            <div className="w-14 h-14 bg-gray-900 rounded-2xl flex items-center justify-center border border-gray-800">
              <Clock className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">Бережное отношение к деталям</h3>
            <p className="text-gray-400 text-sm leading-relaxed">Премиальная бумага, идеальная цветопередача и аккуратная доставка.</p>
          </div>
        </div>
      </section>

      {/* Навигация по услугам (Витрина) */}
      <section id="services-section" className="py-32 px-4">
        <div className="max-w-6xl mx-auto">
          <div className="text-center mb-16">
            <h2 className="text-3xl md:text-4xl font-bold tracking-tight text-white mb-4">Выберите услугу</h2>
            <p className="text-gray-400 text-lg">Загрузите фото и оформите заказ в пару кликов</p>
          </div>

          {/* Табы */}
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-6 mb-16 max-w-5xl mx-auto">
            {services.map((service) => {
              const Icon = service.icon;
              const isActive = activeService === service.id;
              return (
                <button
                  key={service.id}
                  onClick={() => {
                    setActiveService(service.id);
                    setIsOrderSuccess(false);
                  }}
                  className={`group flex flex-col items-center text-center p-8 rounded-3xl cursor-pointer transition-all duration-300 border ${
                    isActive
                      ? 'bg-yellow-400 border-yellow-400 text-black'
                      : 'bg-gray-900 border-gray-800 text-white hover:border-gray-700 hover:bg-gray-800'
                  }`}
                >
                  <Icon className={`w-8 h-8 mb-4 transition-colors ${isActive ? 'text-black' : 'text-gray-400 group-hover:text-white'}`} />
                  <h3 className="text-base font-bold tracking-tight">{service.title}</h3>
                </button>
              );
            })}
          </div>

          {/* Рабочий виджет */}
          <div className="max-w-4xl mx-auto bg-gray-900 rounded-3xl border border-gray-800 p-6 md:p-12 min-h-[500px]">
            {activeService === 'docs' && <PhotoDocWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
            {activeService === 'print' && <PhotoPrintForm onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
            {activeService === 'polaroid' && <PolaroidWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
            {activeService === 'restore' && <RestoreWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
          </div>
        </div>
      </section>

      {/* Секция Калькулятора */}
      {!isOrderSuccess && (
        <section id="calculator-section" className="py-16 px-4 bg-gray-900/30 border-t border-gray-800">
          <div className="max-w-6xl mx-auto">
            <ServiceCalculator />
          </div>
        </section>
      )}

      {/* Подвал (Footer) */}
      <footer id="contacts" className="py-12 border-t border-gray-800">
        <div className="max-w-6xl mx-auto px-4 flex flex-col md:flex-row justify-between items-center md:items-start gap-10 md:gap-6">
          <div className="flex flex-col items-center md:items-start gap-4">
            <p className="text-gray-500 text-sm">© 2026 PhotoDoc AI. Все права защищены.</p>
            <a href="#" className="text-gray-400 hover:text-yellow-400 transition-colors font-medium text-sm py-2 md:py-0">Политика конфиденциальности</a>
          </div>
          <div className="flex flex-col items-center md:items-end gap-4 md:gap-2 text-base md:text-sm">
            <a href="tel:+79132138126" className="text-gray-400 hover:text-yellow-400 transition-colors font-medium p-2 md:p-0">
              +7 (913) 213-81-26
            </a>
            <a href="tel:+79132130605" className="text-gray-400 hover:text-yellow-400 transition-colors font-medium p-2 md:p-0">
              +7 (913) 213-06-05
            </a>
            <a href="mailto:mdot22@yandex.ru" className="text-gray-400 hover:text-yellow-400 transition-colors font-medium p-2 md:p-0">
              mdot22@yandex.ru
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}