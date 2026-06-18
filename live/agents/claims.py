"""A small fact-check benchmark: claims with known truth labels.

Used so a hiring agent can observe whether a delivered verdict was correct and
issue an honest attestation. Real LLMs answer these directly; the label is never
shown to a real provider (only the offline mock backend sees it).
"""
from __future__ import annotations

CLAIMS: list[tuple[str, bool]] = [
    ("The capital of France is Paris.", True),
    ("Water boils at 100 degrees Celsius at sea level.", True),
    ("The Great Wall of China is visible from the Moon with the naked eye.", False),
    ("Mount Everest is the tallest mountain above sea level.", True),
    ("Humans have three lungs.", False),
    ("The Pacific Ocean is the largest ocean on Earth.", True),
    ("Lightning never strikes the same place twice.", False),
    ("The chemical symbol for gold is Au.", True),
    ("Sharks are mammals.", False),
    ("The speed of light is faster than the speed of sound.", True),
    ("Bananas grow on trees.", False),  # they grow on large herbaceous plants
    ("The human body has 206 bones in adulthood.", True),
    ("Mars is the closest planet to the Sun.", False),
    ("Shakespeare wrote Romeo and Juliet.", True),
    ("An octopus has three hearts.", True),
    ("The Sahara is the largest hot desert in the world.", True),
    ("Glass is a slow-moving liquid at room temperature.", False),
    ("The Eiffel Tower is located in Berlin.", False),
    ("Honey never spoils.", True),
    ("The square root of 144 is 12.", True),
    ("The Amazon River is located in Africa.", False),
    ("Sound travels faster in water than in air.", True),
    ("The currency of Japan is the yen.", True),
    ("A leap year occurs every three years.", False),
    ("Oxygen makes up about 21 percent of Earth's atmosphere.", True),
    ("The Mona Lisa was painted by Vincent van Gogh.", False),
    ("DNA stands for deoxyribonucleic acid.", True),
    ("Penguins can fly.", False),
    ("The freezing point of water is 0 degrees Celsius at sea level.", True),
    ("The United States has 50 states.", True),
    ("Spiders are insects.", False),
    ("The heart pumps blood through the body.", True),
    ("Gold is heavier than feathers per unit volume.", True),
    ("The Moon is larger than the Earth.", False),
    ("Photosynthesis occurs in plants.", True),
    ("Mercury is a gas at room temperature.", False),
    ("The Pacific is larger than the Atlantic.", True),
    ("Cats are reptiles.", False),
    ("The Earth orbits the Sun.", True),
    ("A triangle has four sides.", False),
]
