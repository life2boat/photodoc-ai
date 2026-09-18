import React, { useState } from 'react';
import {
  ArrowRight,
  Camera,
  Clock,
  Download,
  Image as ImageIcon,
  Layers3,
  Menu,
  Printer,
  ShieldCheck,
  Star,
  UploadCloud,
  Wand2,
  X
} from 'lucide-react';

// Импортируем все рабочие виджеты
import { PhotoDocWidget } from './components/PhotoDocWidget';
import { PhotoPrintForm } from './components/PhotoPrintForm';
import PolaroidWidget from './components/PolaroidWidget';
import ServiceCalculator from './components/ServiceCalculator'; 
import SliderCompare from './components/SliderCompare';
import { RestoreWidget } from './components/RestoreWidget'; 
import PrivacyModal from './components/PrivacyModal';
import CosmicOrb from './components/ui/CosmicOrb';
import AppearText from './components/ui/AppearText';
import GradientText from './components/ui/GradientText';
import CoverflowGallery from './components/ui/CoverflowGallery';
import InteractiveGrid from './components/ui/InteractiveGrid';
import MagneticButton from './components/ui/MagneticButton';
import FireworkCelebration from './components/ui/FireworkCelebration';
import FadeInSection from './components/ui/FadeInSection';
import heroMockup from './assets/hero-mockup.png';
import passportImg from './assets/img/passport-example.png';
import polaroidImg from './assets/img/polaroid-example.png';
import restorationImg from './assets/img/restoration_placeholder.png';

export default function App() {
  const [activeService, setActiveService] = useState('docs');
  const [isOrderSuccess, setIsOrderSuccess] = useState(false);
  const [isPrivacyOpen, setIsPrivacyOpen] = useState(false);
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);

  const scrollToSection = (id) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  // Услуги для витрины (Реставрация включена)
  const services = [
    { id: 'docs', title: 'Фото на документы', icon: Camera },
    { id: 'print', title: 'Печать фото', icon: Printer },
    { id: 'restore', title: 'Реставрация фото', icon: Wand2 },
    { id: 'polaroid', title: 'Печать Polaroid', icon: ImageIcon },
  ];

  const trustBadges = [
    'Проверка специалистом',
    'Соответствие ГОСТ и визовым требованиям',
    'Готовность в течение 1–2 часов',
    'Поддержка по email и телефону',
  ];

  const exampleCards = [
    {
      title: 'Паспорт',
      subtitle: 'Фото 35×45 • Белый фон',
      image: passportImg,
      mode: 'image',
      fit: 'contain',
    },
    {
      title: 'Реставрация',
      subtitle: 'Восстановление • Цвет и детали',
      image: restorationImg,
      mode: 'image',
    },
    {
      title: 'Polaroid',
      subtitle: 'Ретро-рамка • Атмосферная печать',
      image: polaroidImg,
      mode: 'image',
    },
    {
      title: 'Печать фото',
      subtitle: 'Премиум бумага • Яркие цвета',
      image: heroMockup, // Используем mockup как пример
      mode: 'image',
    },
  ];

  return (
    <div className="min-h-screen bg-[#02040a] text-white font-sans selection:bg-yellow-400 selection:text-black">
      {/* Шапка */}
      <header className="sticky top-0 z-50 border-b border-white/10 bg-black/50 backdrop-blur-md">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-4 md:px-8">
          <button
            type="button"
            onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
            className="text-xl font-bold tracking-tight text-white"
          >
            <GradientText>PhotoDoc</GradientText><span className="text-yellow-400">.</span>
          </button>
          <nav className="hidden items-center gap-6 text-sm font-medium text-zinc-400 md:flex">
            <button type="button" onClick={() => scrollToSection('examples-showcase')} className="transition-colors hover:text-white">
              Примеры
            </button>
            <button type="button" onClick={() => scrollToSection('services-section')} className="transition-colors hover:text-white">
              Услуги
            </button>
            <button type="button" onClick={() => scrollToSection('reviews-section')} className="transition-colors hover:text-white">
              Преимущества
            </button>
            <button type="button" onClick={() => scrollToSection('contacts')} className="transition-colors hover:text-white">
              Контакты
            </button>
          </nav>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => scrollToSection('services-section')}
              className="rounded-full border border-white/10 bg-white/5 px-4 py-2 text-sm font-semibold text-white backdrop-blur-md transition-colors hover:bg-white/10"
            >
              Заказать
            </button>
            <button
              type="button"
              onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
              className="flex h-10 w-10 items-center justify-center rounded-full border border-white/10 bg-white/5 text-zinc-300 md:hidden hover:text-white"
              aria-label={isMobileMenuOpen ? "Закрыть меню" : "Открыть меню"}
              aria-expanded={isMobileMenuOpen}
            >
              {isMobileMenuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
            </button>
          </div>
        </div>

        {/* Мобильная навигация */}
        {isMobileMenuOpen && (
          <nav
            aria-label="Мобильная навигация"
            className="border-t border-white/10 bg-black/95 px-6 py-4 backdrop-blur-xl md:hidden animate-fade-up"
          >
            <div className="flex flex-col gap-3 text-base font-medium text-zinc-300">
              <button
                type="button"
                onClick={() => {
                  scrollToSection('services-section');
                  setIsMobileMenuOpen(false);
                }}
                className="text-left py-2 transition-colors hover:text-white"
              >
                Услуги
              </button>
              <button
                type="button"
                onClick={() => {
                  scrollToSection('examples-showcase');
                  setIsMobileMenuOpen(false);
                }}
                className="text-left py-2 transition-colors hover:text-white"
              >
                Примеры
              </button>
              <button
                type="button"
                onClick={() => {
                  scrollToSection('reviews-section');
                  setIsMobileMenuOpen(false);
                }}
                className="text-left py-2 transition-colors hover:text-white"
              >
                Преимущества
              </button>
              <button
                type="button"
                onClick={() => {
                  scrollToSection('contacts');
                  setIsMobileMenuOpen(false);
                }}
                className="text-left py-2 transition-colors hover:text-white"
              >
                Контакты
              </button>
              <button
                type="button"
                onClick={() => {
                  scrollToSection('services-section');
                  setIsMobileMenuOpen(false);
                }}
                className="mt-2 rounded-xl bg-yellow-400 py-3 text-center font-bold text-black shadow-[0_0_15px_rgba(234,179,8,0.4)]"
              >
                Оформить заказ
              </button>
            </div>
          </nav>
        )}
      </header>

      {/* Hero Секция (Главный экран) */}
      <section className="relative overflow-hidden px-4 pb-24 pt-20 text-center md:pb-32">
        <CosmicOrb />

        <div className="relative mx-auto mt-20 max-w-6xl">
          <div className="animate-fade-up">
            <AppearText
              as="h1"
              text="Цифровая фотостудия нового поколения"
              className="mx-auto mt-6 max-w-[800px] text-5xl font-bold leading-none tracking-tighter text-white md:text-7xl"
              delay={0.2}
            />
            <p className="mx-auto mt-6 max-w-2xl text-base leading-8 text-zinc-400 md:text-xl">
              Подготовьте фото на документы, печать Polaroid или реставрацию снимков с внимательной проверкой
              специалиста и быстрым онлайн-заказом.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <MagneticButton
                onClick={() => scrollToSection('services-section')}
                className="inline-flex w-full items-center justify-center gap-2 rounded-full bg-yellow-400 px-8 py-4 font-bold text-black shadow-[0_0_15px_rgba(234,179,8,0.5)] transition-all hover:scale-105 hover:brightness-110 sm:w-auto"
              >
                Начать заказ
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </MagneticButton>
              <button
                type="button"
                onClick={() => scrollToSection('calculator-section')}
                className="w-full rounded-full border border-white/10 bg-white/5 px-8 py-4 font-semibold text-white backdrop-blur-md transition-colors hover:bg-white/10 sm:w-auto"
              >
                Рассчитать стоимость
              </button>
            </div>
          </div>

          <div className="animate-float-slow relative mx-auto mt-12 w-full max-w-5xl overflow-hidden rounded-2xl border border-white/5 shadow-[0_0_50px_rgba(99,102,241,0.15)]">
            <img
              src={heroMockup}
              alt="Интерфейс цифровой фотостудии: загрузка фото, обработка и готовый результат"
              width="2880"
              height="1064"
              fetchPriority="high"
              loading="eager"
              decoding="sync"
              className="-mt-[2.4%] mx-auto w-full max-w-5xl object-contain"
            />
          </div>

          <div className="mt-8 flex flex-col items-center justify-center gap-3 text-sm text-zinc-400 sm:flex-row sm:flex-wrap sm:gap-4">
            <span>✓ Более 5000 обработанных фото</span>
            <span>✓ Быстрая подготовка и проверка</span>
            <span>✓ Поддержка популярных форматов документов</span>
          </div>

          <div className="mx-auto mt-10 grid max-w-5xl grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {trustBadges.map((badge) => (
              <div key={badge} className="rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 text-sm font-medium text-zinc-200 backdrop-blur-xl">
                ✓ {badge}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Блок преимуществ */}
      <FadeInSection>
        <section id="reviews-section" className="relative scroll-mt-24 py-24 px-4 border-y border-white/10 bg-[#050815] overflow-hidden">
          <InteractiveGrid />
        <h2 className="sr-only">Преимущества PhotoDoc AI</h2>
        <div className="relative z-10 max-w-6xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-5 pointer-events-none">
          <div className="pointer-events-auto group flex flex-col items-center text-center space-y-5 rounded-2xl border border-white/10 bg-white/[0.04] p-8 backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:border-yellow-500/25 hover:shadow-[0_0_40px_rgba(202,138,4,0.10)]">
            <div className="w-16 h-16 bg-yellow-400/10 rounded-2xl flex items-center justify-center border border-yellow-400/20 transition-transform duration-300 group-hover:scale-110">
              <Star className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">Ручная проверка каждого макета</h3>
            <p className="text-zinc-300 text-sm leading-7">Наши специалисты внимательно оценивают качество перед отправкой в печать.</p>
          </div>
          <div className="pointer-events-auto group flex flex-col items-center text-center space-y-5 rounded-2xl border border-white/10 bg-white/[0.04] p-8 backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:border-yellow-500/25 hover:shadow-[0_0_40px_rgba(202,138,4,0.10)]">
            <div className="w-16 h-16 bg-yellow-400/10 rounded-2xl flex items-center justify-center border border-yellow-400/20 transition-transform duration-300 group-hover:scale-110">
              <ShieldCheck className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">Подготовка с учётом требований к документам</h3>
            <p className="text-zinc-300 text-sm leading-7">Идеальные размеры и пропорции для любых видов документов и виз.</p>
          </div>
          <div className="pointer-events-auto group flex flex-col items-center text-center space-y-5 rounded-2xl border border-white/10 bg-white/[0.04] p-8 backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:border-yellow-500/25 hover:shadow-[0_0_40px_rgba(202,138,4,0.10)]">
            <div className="w-16 h-16 bg-yellow-400/10 rounded-2xl flex items-center justify-center border border-yellow-400/20 transition-transform duration-300 group-hover:scale-110">
              <Clock className="w-7 h-7 text-yellow-400" />
            </div>
            <h3 className="text-lg font-bold text-white">Бережное отношение к деталям</h3>
            <p className="text-zinc-300 text-sm leading-7">Премиальная бумага, идеальная цветопередача и аккуратная доставка.</p>
          </div>
          </div>
        </section>
      </FadeInSection>

      {/* Навигация по услугам (Витрина) */}
      <FadeInSection>
        <section id="services-section" className="relative scroll-mt-24 overflow-hidden py-32 px-4">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,rgba(30,58,138,0.20),transparent_42%),radial-gradient(circle_at_80%_35%,rgba(109,40,217,0.10),transparent_30%),linear-gradient(180deg,#02040a_0%,#07101f_48%,#02040a_100%)]" />
        <div className="pointer-events-none absolute left-1/2 top-24 h-96 w-96 -translate-x-1/2 rounded-full bg-blue-500/10 blur-[130px]" />
        <div className="relative max-w-6xl mx-auto">
          <div className="text-center mb-14">
            <h2 className="text-4xl md:text-5xl font-bold tracking-tighter text-white mb-5">Выберите услугу</h2>
            <p className="text-zinc-200 text-lg leading-8 max-w-2xl mx-auto">Загрузите фото, выберите формат и получите готовый результат без лишних форм и переписок.</p>
          </div>

          <div className="mx-auto mb-12 grid max-w-5xl grid-cols-1 gap-3 rounded-2xl border border-white/10 bg-white/5 p-3 shadow-[0_0_40px_rgba(15,23,42,0.55)] backdrop-blur-md md:grid-cols-[1fr_auto_1fr_auto_1fr] md:items-center">
            {[
              { icon: UploadCloud, title: '1. Загрузите фото', text: 'JPG, PNG или WEBP' },
              { icon: Wand2, title: '2. Мы обрабатываем', text: 'Улучшение качества и подготовка' },
              { icon: Download, title: '3. Получите результат', text: 'Печать или цифровой заказ' }
            ].map((step, index) => {
              const Icon = step.icon;
              const iconTone = index === 1
                ? 'border-violet-300/20 bg-violet-400/10 text-violet-200'
                : 'border-yellow-500/20 bg-yellow-500/10 text-yellow-300';
              return (
                <React.Fragment key={step.title}>
                  <div className="flex items-center gap-3 rounded-xl bg-[#050914]/80 p-4">
                    <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border ${iconTone}`}>
                      <Icon className="h-5 w-5" aria-hidden="true" />
                    </div>
                    <div className="text-left">
                      <p className="text-sm font-bold text-white">{step.title}</p>
                      <p className="mt-1 text-xs text-zinc-300">{step.text}</p>
                    </div>
                  </div>
                  {index < 2 && <div className="hidden w-10 border-t border-dotted border-white/25 md:block" />}
                </React.Fragment>
              );
            })}
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
                  className={`group relative flex flex-col items-center text-center p-7 rounded-xl cursor-pointer transition-all duration-300 border backdrop-blur-xl hover:-translate-y-1 ${
                    isActive
                      ? 'bg-[linear-gradient(135deg,rgba(8,13,28,0.95),rgba(13,24,45,0.82))] border-yellow-500/50 text-white shadow-[0_0_40px_rgba(202,138,4,0.12)]'
                      : 'bg-[linear-gradient(135deg,rgba(255,255,255,0.045),rgba(30,58,138,0.08))] border-white/5 text-white hover:border-white/15 hover:shadow-[0_18px_50px_rgba(0,0,0,0.32)]'
                  }`}
                >
                  {isActive && <span className="absolute inset-x-8 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/80 to-transparent" />}
                  <div className={`mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border transition-all duration-300 ${
                    isActive
                      ? 'border-yellow-500/30 bg-yellow-500/10 shadow-[0_0_32px_rgba(202,138,4,0.16)]'
                      : 'border-white/10 bg-white/5 group-hover:border-yellow-500/20 group-hover:bg-yellow-500/10'
                  }`}>
                    <Icon className={`w-7 h-7 transition-colors ${isActive ? 'text-yellow-300' : 'text-zinc-300 group-hover:text-yellow-200'}`} />
                  </div>
                  <h3 className="text-base font-bold tracking-tight">{service.title}</h3>
                </button>
              );
            })}
          </div>

          {/* Рабочий виджет */}
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px] lg:items-start">
            <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-[linear-gradient(145deg,rgba(255,255,255,0.055),rgba(15,23,42,0.38))] p-5 shadow-[0_0_60px_rgba(15,23,42,0.55)] backdrop-blur-xl md:p-8">
              <div className="pointer-events-none absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/30 to-transparent" />
              {activeService === 'docs' && <PhotoDocWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
              {activeService === 'print' && <PhotoPrintForm onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
              {activeService === 'polaroid' && <PolaroidWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
              {activeService === 'restore' && <RestoreWidget onSuccess={() => setIsOrderSuccess(true)} onReset={() => setIsOrderSuccess(false)} />}
            </div>

            <aside className="animate-float-slow rounded-3xl border border-white/10 bg-zinc-900/50 p-5 shadow-[0_0_40px_rgba(79,70,229,0.10)] backdrop-blur-xl">
              <div className="mb-4 flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl border border-violet-300/20 bg-violet-400/10">
                  <Layers3 className="h-5 w-5 text-violet-200" aria-hidden="true" />
                </div>
                <div>
                  <p className="text-sm font-bold text-white">Предпросмотр</p>
                  <p className="text-xs text-zinc-300">Оригинал → результат</p>
                </div>
              </div>
              <SliderCompare />
              <div className="mt-4 grid grid-cols-2 gap-3 text-xs text-zinc-300">
                <div className="rounded-full border border-white/10 bg-black/30 px-3 py-2">Фон: белый</div>
                <div className="rounded-full border border-white/10 bg-black/30 px-3 py-2">Резкость: улучшена</div>
                <div className="rounded-full border border-white/10 bg-black/30 px-3 py-2">Размер: ГОСТ</div>
                <div className="rounded-full border border-white/10 bg-black/30 px-3 py-2">Проверка вручную</div>
              </div>
            </aside>
          </div>
        </div>
        </section>
      </FadeInSection>

      <FadeInSection>
        <section id="examples-showcase" className="relative overflow-hidden px-4 py-24">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_18%,rgba(30,58,138,0.18),transparent_36%),radial-gradient(circle_at_15%_72%,rgba(202,138,4,0.08),transparent_28%)]" />
        <div className="relative mx-auto max-w-6xl">
          <div className="mb-12 text-center">
            <h2 className="text-3xl font-bold tracking-tighter text-white md:text-4xl">Примеры работ</h2>
            <p className="mx-auto mt-4 max-w-2xl text-base leading-7 text-zinc-300">
              Визуальные сценарии для самых популярных услуг: аккуратный свет, чистый фон и готовый результат без лишней суеты.
            </p>
          </div>

          <CoverflowGallery cards={exampleCards} />
        </div>
      </section>
      </FadeInSection>

      {/* Секция Калькулятора */}
      {isOrderSuccess && <FireworkCelebration />}
      {!isOrderSuccess && (
        <FadeInSection>
          <section id="calculator-section" className="scroll-mt-24 py-16 px-4 bg-gray-900/30 border-t border-gray-800">
            <div className="max-w-6xl mx-auto">
              <ServiceCalculator />
            </div>
          </section>
        </FadeInSection>
      )}

      {/* Подвал (Footer) */}
      <footer id="contacts" className="scroll-mt-24 py-12 border-t border-gray-800">
        <div className="max-w-6xl mx-auto px-4 flex flex-col md:flex-row justify-between items-center md:items-start gap-10 md:gap-6">
          <div className="flex flex-col items-center md:items-start gap-4">
            <p className="text-gray-500 text-sm">© 2026 PhotoDoc AI. Все права защищены.</p>
            <button
              type="button"
              onClick={() => setIsPrivacyOpen(true)}
              className="text-gray-400 hover:text-yellow-400 transition-colors font-medium text-sm py-2 md:py-0"
            >
              Политика конфиденциальности
            </button>
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
      <PrivacyModal isOpen={isPrivacyOpen} onClose={() => setIsPrivacyOpen(false)} />
    </div>
  );
}
