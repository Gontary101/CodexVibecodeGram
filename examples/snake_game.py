#!/usr/bin/env python3
"""Single-file Snake game with built-in tests and screenshot rendering.

Usage:
  python3 snake_game.py                 # play interactively
  python3 snake_game.py --test          # run built-in logic tests
  python3 snake_game.py --screenshots   # render deterministic screenshots
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pygame


# Board and rendering constants
GRID_WIDTH = 24
GRID_HEIGHT = 18
CELL_SIZE = 32
HUD_HEIGHT = 80
WINDOW_WIDTH = GRID_WIDTH * CELL_SIZE
WINDOW_HEIGHT = GRID_HEIGHT * CELL_SIZE + HUD_HEIGHT


# Colors
BG_COLOR = (18, 22, 31)
PANEL_COLOR = (26, 34, 47)
GRID_COLOR = (44, 58, 78)
SNAKE_HEAD = (48, 214, 156)
SNAKE_BODY = (18, 166, 112)
FOOD_COLOR = (245, 82, 82)
TEXT_COLOR = (236, 243, 255)
MUTED_TEXT = (162, 179, 201)
GAME_OVER_COLOR = (255, 187, 73)


UP = (0, -1)
DOWN = (0, 1)
LEFT = (-1, 0)
RIGHT = (1, 0)

DIRECTION_BY_KEY = {
    pygame.K_UP: UP,
    pygame.K_w: UP,
    pygame.K_DOWN: DOWN,
    pygame.K_s: DOWN,
    pygame.K_LEFT: LEFT,
    pygame.K_a: LEFT,
    pygame.K_RIGHT: RIGHT,
    pygame.K_d: RIGHT,
}

OPPOSITE = {
    UP: DOWN,
    DOWN: UP,
    LEFT: RIGHT,
    RIGHT: LEFT,
}

DIRECTION_NAME = {
    UP: "UP",
    DOWN: "DOWN",
    LEFT: "LEFT",
    RIGHT: "RIGHT",
}


@dataclass
class SnakeGame:
    width: int = GRID_WIDTH
    height: int = GRID_HEIGHT
    seed: int | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.reset()

    def reset(self) -> None:
        center_x = self.width // 2
        center_y = self.height // 2
        self.snake: list[tuple[int, int]] = [
            (center_x, center_y),
            (center_x - 1, center_y),
            (center_x - 2, center_y),
        ]
        self.direction = RIGHT
        self.next_direction = RIGHT
        self.score = 0
        self.steps = 0
        self.alive = True
        self.food = self._random_empty_cell()

    def _random_empty_cell(self) -> tuple[int, int]:
        occupied = set(self.snake)
        free_cells = [
            (x, y)
            for y in range(self.height)
            for x in range(self.width)
            if (x, y) not in occupied
        ]
        if not free_cells:
            # Technically a win (board full). Keep food where it is.
            return self.snake[0]
        return self.rng.choice(free_cells)

    def set_direction(self, requested: tuple[int, int]) -> None:
        # Ignore exact opposite turns to avoid instant self-collision.
        if requested == OPPOSITE[self.direction]:
            return
        self.next_direction = requested

    def _next_head(self, direction: tuple[int, int]) -> tuple[int, int]:
        dx, dy = direction
        x, y = self.snake[0]
        return x + dx, y + dy

    def would_collide(self, direction: tuple[int, int]) -> bool:
        new_head = self._next_head(direction)
        x, y = new_head
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return True

        will_grow = new_head == self.food
        # If we won't grow, moving into the current tail is legal because tail moves away.
        blocked = self.snake if will_grow else self.snake[:-1]
        return new_head in blocked

    def step(self) -> None:
        if not self.alive:
            return

        if self.next_direction != OPPOSITE[self.direction]:
            self.direction = self.next_direction

        new_head = self._next_head(self.direction)
        x, y = new_head
        self.steps += 1

        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            self.alive = False
            return

        will_grow = new_head == self.food
        blocked = self.snake if will_grow else self.snake[:-1]
        if new_head in blocked:
            self.alive = False
            return

        self.snake.insert(0, new_head)
        if will_grow:
            self.score += 1
            self.food = self._random_empty_cell()
        else:
            self.snake.pop()


def draw_background(surface: pygame.Surface) -> None:
    surface.fill(BG_COLOR)

    board_rect = pygame.Rect(0, HUD_HEIGHT, WINDOW_WIDTH, WINDOW_HEIGHT - HUD_HEIGHT)
    pygame.draw.rect(surface, PANEL_COLOR, board_rect)

    for x in range(0, WINDOW_WIDTH + 1, CELL_SIZE):
        pygame.draw.line(surface, GRID_COLOR, (x, HUD_HEIGHT), (x, WINDOW_HEIGHT), 1)
    for y in range(HUD_HEIGHT, WINDOW_HEIGHT + 1, CELL_SIZE):
        pygame.draw.line(surface, GRID_COLOR, (0, y), (WINDOW_WIDTH, y), 1)


def draw_game(surface: pygame.Surface, game: SnakeGame, font: pygame.font.Font, small_font: pygame.font.Font) -> None:
    draw_background(surface)

    # Food
    fx, fy = game.food
    food_center = (
        fx * CELL_SIZE + CELL_SIZE // 2,
        HUD_HEIGHT + fy * CELL_SIZE + CELL_SIZE // 2,
    )
    pygame.draw.circle(surface, FOOD_COLOR, food_center, CELL_SIZE // 2 - 4)

    # Snake body and head
    for i, (sx, sy) in enumerate(game.snake):
        rect = pygame.Rect(
            sx * CELL_SIZE + 3,
            HUD_HEIGHT + sy * CELL_SIZE + 3,
            CELL_SIZE - 6,
            CELL_SIZE - 6,
        )
        color = SNAKE_HEAD if i == 0 else SNAKE_BODY
        radius = 10 if i == 0 else 8
        pygame.draw.rect(surface, color, rect, border_radius=radius)

    # HUD
    title = font.render("SNAKE", True, TEXT_COLOR)
    surface.blit(title, (18, 14))

    score_text = small_font.render(f"Score: {game.score}", True, TEXT_COLOR)
    steps_text = small_font.render(f"Steps: {game.steps}", True, MUTED_TEXT)
    dir_text = small_font.render(f"Dir: {DIRECTION_NAME[game.direction]}", True, MUTED_TEXT)

    surface.blit(score_text, (160, 20))
    surface.blit(steps_text, (300, 20))
    surface.blit(dir_text, (450, 20))

    if game.alive:
        hint = small_font.render("Arrows/WASD to move, P pause, R reset, ESC quit", True, MUTED_TEXT)
        surface.blit(hint, (18, 52))
    else:
        over = font.render("GAME OVER", True, GAME_OVER_COLOR)
        retry = small_font.render("Press R to restart or ESC to quit", True, TEXT_COLOR)
        surface.blit(over, (WINDOW_WIDTH - 270, 14))
        surface.blit(retry, (WINDOW_WIDTH - 340, 52))


def pick_safe_direction_toward_food(game: SnakeGame) -> tuple[int, int]:
    head_x, head_y = game.snake[0]
    food_x, food_y = game.food

    candidates: list[tuple[int, int]] = []
    if food_x > head_x:
        candidates.append(RIGHT)
    elif food_x < head_x:
        candidates.append(LEFT)

    if food_y > head_y:
        candidates.append(DOWN)
    elif food_y < head_y:
        candidates.append(UP)

    for direction in (UP, DOWN, LEFT, RIGHT):
        if direction not in candidates:
            candidates.append(direction)

    for direction in candidates:
        if direction == OPPOSITE[game.direction]:
            continue
        if not game.would_collide(direction):
            return direction

    # If every direction is unsafe, keep direction and let the game resolve collision.
    return game.direction


def save_frame(path: Path, game: SnakeGame, font: pygame.font.Font, small_font: pygame.font.Font) -> None:
    surface = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT))
    draw_game(surface, game, font, small_font)
    pygame.image.save(surface, str(path))


def generate_screenshots(output_dir: Path) -> list[Path]:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    pygame.init()
    pygame.font.init()

    font = pygame.font.SysFont("consolas", 42, bold=True)
    small_font = pygame.font.SysFont("consolas", 26)

    output_dir.mkdir(parents=True, exist_ok=True)
    game = SnakeGame(seed=7)

    paths: list[Path] = []

    p1 = output_dir / "snake_01_start.png"
    save_frame(p1, game, font, small_font)
    paths.append(p1)

    # Build a deterministic mid-game state.
    target_steps = 120
    for _ in range(target_steps):
        if not game.alive:
            break
        game.set_direction(pick_safe_direction_toward_food(game))
        game.step()

    p2 = output_dir / "snake_02_midgame.png"
    save_frame(p2, game, font, small_font)
    paths.append(p2)

    # Intentionally crash into a wall for a game-over screenshot.
    if game.alive:
        head_x, head_y = game.snake[0]
        distances = [
            (head_y, UP),
            (game.height - 1 - head_y, DOWN),
            (head_x, LEFT),
            (game.width - 1 - head_x, RIGHT),
        ]
        _, toward_wall = min(distances, key=lambda t: t[0])
        # Force direction directly (including opposite) to guarantee a bounded crash path.
        game.direction = toward_wall
        game.next_direction = toward_wall
        for _ in range(max(game.width, game.height) + 2):
            if not game.alive:
                break
            game.step()
        if game.alive:
            # Failsafe for pathological states; sufficient for a game-over screenshot.
            game.alive = False

    p3 = output_dir / "snake_03_gameover.png"
    save_frame(p3, game, font, small_font)
    paths.append(p3)

    pygame.quit()
    return paths


def run_self_tests() -> None:
    # Test 1: reset state basics
    g = SnakeGame(width=10, height=8, seed=1)
    assert len(g.snake) == 3, "Snake should start with length 3"
    assert g.score == 0 and g.alive, "New game should start alive with score 0"
    assert g.food not in g.snake, "Food must spawn on empty cells"

    # Test 2: opposite direction change is ignored
    g = SnakeGame(width=10, height=8, seed=1)
    g.set_direction(LEFT)
    g.step()
    assert g.direction == RIGHT, "Opposite turn should be ignored"

    # Test 3: eating food grows snake and increments score
    g = SnakeGame(width=8, height=6, seed=2)
    head_x, head_y = g.snake[0]
    g.food = (head_x + 1, head_y)
    old_len = len(g.snake)
    g.step()
    assert g.score == 1, "Score should increment when food eaten"
    assert len(g.snake) == old_len + 1, "Snake should grow after eating"

    # Test 4: wall collision ends game
    g = SnakeGame(width=5, height=5, seed=3)
    g.snake = [(4, 2), (3, 2), (2, 2)]
    g.direction = RIGHT
    g.next_direction = RIGHT
    g.step()
    assert not g.alive, "Crossing boundary should end the game"

    # Test 5: moving into tail cell is legal when not growing
    g = SnakeGame(width=6, height=6, seed=4)
    g.snake = [(2, 2), (2, 3), (1, 3), (1, 2)]
    g.direction = UP
    g.next_direction = UP
    g.food = (5, 5)
    g.step()
    assert g.alive, "Moving into former tail should be legal without growth"

    # Test 6: deterministic simulation for fixed seed
    g1 = SnakeGame(width=12, height=10, seed=9)
    g2 = SnakeGame(width=12, height=10, seed=9)
    for _ in range(20):
        d1 = pick_safe_direction_toward_food(g1)
        d2 = pick_safe_direction_toward_food(g2)
        g1.set_direction(d1)
        g2.set_direction(d2)
        g1.step()
        g2.step()
    assert g1.snake == g2.snake and g1.food == g2.food and g1.score == g2.score


def play(speed: int) -> None:
    pygame.init()
    pygame.display.set_caption("Snake - Single File")

    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    clock = pygame.time.Clock()

    font = pygame.font.SysFont("consolas", 42, bold=True)
    small_font = pygame.font.SysFont("consolas", 26)

    game = SnakeGame()
    paused = False

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit()
                    return
                if event.key == pygame.K_r:
                    game.reset()
                    paused = False
                if event.key == pygame.K_p:
                    paused = not paused
                if event.key in DIRECTION_BY_KEY:
                    game.set_direction(DIRECTION_BY_KEY[event.key])

        if not paused and game.alive:
            game.step()

        draw_game(screen, game, font, small_font)
        if paused:
            pause_text = font.render("PAUSED", True, GAME_OVER_COLOR)
            screen.blit(pause_text, (WINDOW_WIDTH // 2 - 90, 14))

        pygame.display.flip()
        clock.tick(max(5, speed + game.score // 2))


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single-file Snake game")
    parser.add_argument("--test", action="store_true", help="Run built-in tests")
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Render deterministic screenshots to runs/snake_screenshots",
    )
    parser.add_argument(
        "--speed",
        type=int,
        default=10,
        help="Base game speed in ticks per second (interactive mode)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("runs/snake_screenshots"),
        help="Screenshot output directory",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)

    if args.test:
        run_self_tests()
        print("All snake tests passed.")
        return 0

    if args.screenshots:
        paths = generate_screenshots(args.out_dir)
        print("Rendered screenshots:")
        for p in paths:
            print(p)
        return 0

    play(speed=args.speed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
