/**
  ******************************************************************************
  * @file           : bme280.h
  * @brief          : Minimal BME280 (temp / pressure / humidity) driver, HAL I2C.
  ******************************************************************************
  * @attention
  *
  * This is a small single-file port for the Fleet edge firmware. The register
  * map, config sequence, and - importantly - the compensation formulas are the
  * Bosch reference: every BME280 is individually factory-trimmed, so raw ADC
  * counts are meaningless until run through per-chip NVM coefficients. We read
  * those coefficients once at init and apply Bosch's published fixed-point
  * compensation (see bme280.c) rather than deriving the math ourselves.
  *
  * Datasheet: Bosch BME280, rev 1.6. Compensation code adapted verbatim from
  * the datasheet appendix (BME280_compensate_*_int / _int64 reference).
  ******************************************************************************
  */
#ifndef INC_BME280_H_
#define INC_BME280_H_

#include "stm32f4xx_hal.h"

/* 7-bit I2C address left-shifted for the HAL (SDO->GND = 0x76, SDO->VDD = 0x77). */
#define BME280_I2C_ADDR_PRIM   (0x76 << 1)
#define BME280_I2C_ADDR_SEC    (0x77 << 1)

/* Per-chip calibration coefficients read out of the sensor's NVM at init. Names
   match the datasheet (dig_T*, dig_P*, dig_H*). t_fine carries the fine
   temperature between the T, P and H compensation steps, exactly as Bosch does. */
typedef struct
{
  uint16_t dig_T1;
  int16_t  dig_T2;
  int16_t  dig_T3;

  uint16_t dig_P1;
  int16_t  dig_P2;
  int16_t  dig_P3;
  int16_t  dig_P4;
  int16_t  dig_P5;
  int16_t  dig_P6;
  int16_t  dig_P7;
  int16_t  dig_P8;
  int16_t  dig_P9;

  uint8_t  dig_H1;
  int16_t  dig_H2;
  uint8_t  dig_H3;
  int16_t  dig_H4;
  int16_t  dig_H5;
  int8_t   dig_H6;

  int32_t  t_fine;
} BME280_Calib_t;

/* One driver instance = one sensor on one bus. */
typedef struct
{
  I2C_HandleTypeDef *hi2c;   /* which HAL I2C peripheral */
  uint16_t           addr;   /* BME280_I2C_ADDR_PRIM/_SEC (already << 1) */
  BME280_Calib_t     calib;  /* factory trim, filled by BME280_Init */
} BME280_t;

/**
  * @brief  Read the NVM calibration and configure the sensor for continuous
  *         measurement (humidity/temp/pressure oversampling x1, normal mode).
  * @note   Call once before sampling. Writes ctrl_hum before ctrl_meas so the
  *         humidity oversampling latches (a documented ordering gotcha).
  * @retval HAL_OK on success, HAL error code otherwise.
  */
HAL_StatusTypeDef BME280_Init(BME280_t *dev, I2C_HandleTypeDef *hi2c, uint16_t addr);

/**
  * @brief  Burst-read the raw data registers and apply Bosch compensation.
  * @param  temperature_c  out: degrees Celsius
  * @param  pressure_hpa   out: hectopascals (hPa == mbar)
  * @param  humidity_pct   out: %RH
  * @retval HAL_OK on success, HAL error code otherwise.
  */
HAL_StatusTypeDef BME280_Read(BME280_t *dev,
                              float *temperature_c,
                              float *pressure_hpa,
                              float *humidity_pct);

#endif /* INC_BME280_H_ */
