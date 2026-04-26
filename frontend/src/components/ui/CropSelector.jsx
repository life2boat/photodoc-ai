import React from 'react';
import { Maximize, Minimize, UserCog } from 'lucide-react';

const CROP_OPTIONS = [
  {
    id: 'fill',
    label: 'Без полей',
    description: 'Фото заполнит всю бумагу. Часть изображения по краям обрежется.',
    icon: Maximize
  },
  {
    id: 'fit',
    label: 'С полями',
    description: 'Фото поместится целиком, но по краям останутся белые полосы.',
    icon: Minimize
  },
  {
    id: 'auto',
    label: 'На усмотрение оператора',
    description: 'Мы сами выберем лучший вариант, чтобы не обрезать важные детали.',
    icon: UserCog
  }
];

export function CropSelector({ cropMode = 'fill', onChange }) {
  return (
    <div className="space-y-4 w-full">
      <label className="text-sm font-medium text-neutral-900 block">Режим кадрирования</label>
      <div className="grid gap-4 md:grid-cols-3">
        {CROP_OPTIONS.map((option) => {
          const Icon = option.icon;
          const isSelected = cropMode === option.id;
          
          return (
            <div
              key={option.id}
              onClick={() => onChange(option.id)}
              className={`relative flex flex-col p-4 cursor-pointer rounded-lg border-2 transition-all duration-200 ${
                isSelected
                  ? 'border-primary bg-primary/5 shadow-sm'
                  : 'border-neutral-200 bg-white hover:border-primary/50'
              }`}
            >
              <div className="flex items-center mb-2">
                <Icon className={`w-5 h-5 mr-2 ${isSelected ? 'text-primary' : 'text-neutral-500'}`} />
                <span className={`font-semibold ${isSelected ? 'text-primary' : 'text-neutral-700'}`}>
                  {option.label}
                </span>
              </div>
              <p className="text-xs text-neutral-500 leading-relaxed">
                {option.description}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}