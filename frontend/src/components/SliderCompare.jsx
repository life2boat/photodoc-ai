import React, { useState } from 'react';
import { ArrowLeftRight } from 'lucide-react';
import beforeImage from '../assets/slider-before.webp';
import afterImage from '../assets/slider-after.webp';

export default function SliderCompare() {
  const [position, setPosition] = useState(50);

  return (
    <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-zinc-950 shadow-[0_0_40px_rgba(202,138,4,0.10)]">
      <div className="relative aspect-[4/5] select-none">
        <img
          src={afterImage}
          alt="Фото после обработки"
          className="absolute inset-0 h-full w-full object-cover"
          draggable="false"
          loading="lazy"
          decoding="async"
        />
        <div className="absolute inset-0 overflow-hidden" style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}>
          <img
            src={beforeImage}
            alt="Фото до обработки"
            className="h-full w-full object-cover"
            draggable="false"
            loading="lazy"
            decoding="async"
          />
        </div>

        <div className="absolute left-3 top-3 rounded-full bg-zinc-900/80 px-2.5 py-1 text-xs font-medium text-zinc-100 backdrop-blur-md">
          До
        </div>
        <div className="absolute right-3 top-3 rounded-full bg-zinc-900/80 px-2.5 py-1 text-xs font-medium text-zinc-100 backdrop-blur-md">
          После
        </div>

        <div
          className="pointer-events-none absolute inset-y-0 w-px bg-yellow-500 shadow-[0_0_18px_rgba(234,179,8,0.45)]"
          style={{ left: `${position}%` }}
        />
        <div
          className="pointer-events-none absolute top-1/2 flex h-11 w-11 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/10 bg-black/80 text-yellow-300 shadow-[0_0_30px_rgba(202,138,4,0.22)] backdrop-blur-md"
          style={{ left: `${position}%` }}
        >
          <ArrowLeftRight className="h-5 w-5" aria-hidden="true" />
        </div>

        <input
          type="range"
          min="0"
          max="100"
          value={position}
          onChange={(event) => setPosition(Number(event.target.value))}
          aria-label="Сравнить фото до и после"
          className="absolute inset-0 h-full w-full cursor-ew-resize opacity-0"
        />
      </div>
    </div>
  );
}
