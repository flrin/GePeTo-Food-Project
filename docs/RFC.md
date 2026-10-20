# RFC: LLM-Powered Nutrition Assistant

## 1. Problem Definition

People who want to lose weight, gain muscle, or maintain their weight often track what they eat, but the numbers do not always help them decide what to eat next. A meal might fit someone's goals differently depending on the portion size and what they have already eaten. Food preferences also matter, since a recommendation is not very useful when the user does not like the suggested meal.

This project aims to make these decisions easier by combining food logging with short, personalized explanations and meal suggestions.

## 2. State of the Art

MyFitnessPal helps users record meals and track calories and macronutrients. It is useful for keeping a food diary, but users still need to decide how to apply that information to their next meal.

MacroFactor adjusts calorie and macronutrient targets using logged intake and weight changes. This helps users follow their progress, but targets alone do not answer every question about which foods to choose.

General-purpose LLMs can suggest meals and explain nutritional information in everyday language. However, a conversation on its own is not a dedicated food diary or a reliable way to calculate nutrient totals. Our project would combine structured food records with this kind of conversational guidance.

## 3. Proposed Solution

We propose a nutrition app where users first enter their height, weight, age, activity level, food preferences, and fitness goals. The app would use this profile to estimate daily calorie and macronutrient targets.

Users would log their meals, and the app would keep a history of their intake. Nutritional values would come from a food database where possible, with estimates marked clearly. The LLM would use this information to give short food descriptions and suggest meals.

Users could also ask whether a food fits their plan before eating it. The answer would consider their targets, previous meals, dietary restrictions, and feedback on earlier suggestions. For example, it could suggest a smaller portion or a different side dish, rather than simply calling the food good or bad. If a user rejects a suggestion because they dislike an ingredient, later recommendations should take that into account.

The main difference would be keeping food tracking and advice together, so users would not have to explain their goals and previous meals each time they ask a question. The first version would focus on profiles, food logging, calorie and macronutrient tracking, and basic recommendations. More detailed meal planning could be added later.
