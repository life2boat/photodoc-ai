import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronLeft, ChevronRight } from 'lucide-react';

export default function CoverflowGallery({ cards }) {
  const [currentIndex, setCurrentIndex] = useState(0);

  const handleNext = () => {
    setCurrentIndex((prevIndex) => (prevIndex + 1) % cards.length);
  };

  const handlePrev = () => {
    setCurrentIndex((prevIndex) => (prevIndex - 1 + cards.length) % cards.length);
  };

  return (
    <div className="relative mx-auto flex h-[500px] w-full max-w-5xl flex-col items-center justify-center perspective-[1200px]">
      <div className="relative flex h-full w-full items-center justify-center preserve-3d">
        <AnimatePresence initial={false} mode="popLayout">
          {cards.map((card, index) => {
            // Determine relative position: -1 is left, 0 is center, 1 is right.
            const offset = (index - currentIndex + cards.length) % cards.length;
            const normalizedOffset = offset > cards.length / 2 ? offset - cards.length : offset;

            // Only render adjacent cards or center card
            if (Math.abs(normalizedOffset) > 2) return null;

            const isCenter = normalizedOffset === 0;
            const xOffset = normalizedOffset * 180;
            const zOffset = Math.abs(normalizedOffset) * -150;
            const rotateY = normalizedOffset * -30;
            const opacity = 1 - Math.abs(normalizedOffset) * 0.3;

            return (
              <motion.article
                key={card.title}
                layout
                initial={{ opacity: 0, x: xOffset, z: -300 }}
                animate={{
                  opacity,
                  x: xOffset,
                  z: zOffset,
                  rotateY,
                  scale: isCenter ? 1.05 : 0.9,
                }}
                exit={{ opacity: 0, x: xOffset, z: -300 }}
                transition={{
                  type: 'spring',
                  stiffness: 260,
                  damping: 20,
                  mass: 1,
                }}
                style={{
                  position: 'absolute',
                  zIndex: cards.length - Math.abs(normalizedOffset),
                }}
                className={`group cursor-pointer overflow-hidden rounded-2xl border border-white/10 bg-white/5 backdrop-blur-md transition-shadow hover:border-yellow-500/30 hover:shadow-[0_0_44px_rgba(202,138,4,0.12)] w-[320px] sm:w-[400px]`}
                onClick={() => {
                  if (!isCenter) setCurrentIndex(index);
                }}
              >
                <div className={`relative overflow-hidden bg-[radial-gradient(circle_at_50%_18%,rgba(255,255,255,0.10),transparent_26%),linear-gradient(145deg,#0a1020,#02040a)] aspect-[4/5]`}>
                  <img
                    src={card.image}
                    alt={card.title}
                    loading="lazy"
                    className={`h-full w-full transition-transform duration-500 ease-in-out group-hover:scale-[1.03] ${
                      card.fit === 'contain' ? 'object-contain p-4' : 'object-cover'
                    }`}
                  />
                  <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-white/5 opacity-90" />
                </div>
                <div className="absolute bottom-0 w-full border-t border-white/10 bg-black/40 p-5 text-center backdrop-blur-md">
                  <h3 className="text-lg font-bold text-white">{card.title}</h3>
                  <p className="mt-1 text-sm leading-6 text-zinc-300">{card.subtitle}</p>
                </div>
              </motion.article>
            );
          })}
        </AnimatePresence>
      </div>

      <div className="mt-8 flex items-center justify-center gap-6">
        <button
          type="button"
          onClick={handlePrev}
          className="flex h-12 w-12 items-center justify-center rounded-full border border-white/10 bg-white/5 text-white backdrop-blur-md transition-all hover:bg-white/10 hover:scale-110 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-yellow-400"
          aria-label="РџСЂРµРґС‹РґСѓС‰РµРµ С„РѕС‚Рѕ"
        >
          <ChevronLeft className="h-6 w-6" />
        </button>
        <div className="flex items-center gap-1">
          {cards.map((_, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setCurrentIndex(i)}
              className="group flex h-11 min-w-[28px] items-center justify-center px-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-yellow-400 rounded-full"
              aria-label={`РџРµСЂРµР№С‚Рё Рє СЃР»Р°Р№РґСѓ ${i + 1}`}
            >
              <span
                className={`h-2 rounded-full transition-all ${
                  i === currentIndex ? 'w-8 bg-yellow-400' : 'w-2.5 bg-white/20 group-hover:bg-white/40'
                }`}
              />
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={handleNext}
          className="flex h-12 w-12 items-center justify-center rounded-full border border-white/10 bg-white/5 text-white backdrop-blur-md transition-all hover:bg-white/10 hover:scale-110 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-yellow-400"
          aria-label="РЎР»РµРґСѓСЋС‰РµРµ С„РѕС‚Рѕ"
        >
          <ChevronRight className="h-6 w-6" />
        </button>
      </div>
    </div>
  );
}
