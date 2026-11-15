# RFC: LLM-Powered Nutrition Assistant

## 1. Problem Definition

A lot of people who want to lose weight, gain muscle or just stay where they are write down what they eat. The problem we noticed is that the numbers alone don't really tell you what to eat next. The same meal can fit your goals very differently depending on how big the portion is and what you already ate earlier that day. Taste matters too: if the app suggests something you don't like, you will just ignore it.

With this project we want to make that decision a bit easier. The idea is to put food logging and short, personal explanations or meal suggestions in the same place.

## 2. State of the Art

We looked at a few existing tools to see what is already out there.

MyFitnessPal lets users record meals and track calories and macronutrients (protein, carbs, fat). It works well as a food diary, but the user still has to figure out by themselves what the numbers mean for the next meal.

MacroFactor changes the calorie and macro targets based on what the user logs and how their weight changes. That helps people follow their progress, but a target by itself doesn't say which foods to pick.

General-purpose LLMs such as ChatGPT can suggest meals and explain nutrition in simple language. The catch is that a chat is not a food diary, and we are not sure we can trust a language model to add up nutrient totals correctly. Our project would try to combine structured food records (the diary part) with the conversational part.

## 3. Proposed Solution

We want to build a nutrition app. First, the user enters their height, weight, age, activity level, food preferences and fitness goals. The app uses this profile to estimate daily calorie and macronutrient targets.

After that the user logs their meals and the app keeps a history of what they ate. Nutrient values should come from a food database whenever possible, and anything that is only an estimate should be clearly marked as one. The LLM would then use this information to write short descriptions of foods and to suggest meals.

Users could also ask whether a food fits their plan *before* eating it. The answer should look at their targets, the meals already logged, any dietary restrictions and how they reacted to earlier suggestions. For example, instead of just saying "good" or "bad", the app could suggest a smaller portion or a different side dish. If the user rejects a suggestion because they dislike an ingredient, later recommendations should remember that.

What we think makes this different is that tracking and advice live together. The user would not need to explain their goals and what they ate every time they ask something. For the first version we want to keep the scope small: profiles, food logging, calorie and macronutrient tracking, and basic recommendations. Proper meal planning is something we could add later if there is time.
