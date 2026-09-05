export const characters = [
  "Monk",
  "Villager",
  "Villager2",
  "Villager3",
  "Villager4",
  "Villager5",
  "Boy",
  "Hunter",
  "Noble",
  "OldMan",
  "Princess",
  "Woman",
];
export function characterName(value) {
  return characters.includes(value) ? value : "Villager";
}
