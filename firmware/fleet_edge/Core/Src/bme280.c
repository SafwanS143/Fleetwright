/**
  ******************************************************************************
  * @file           : bme280.c
  * @brief          : Minimal BME280 driver (HAL I2C) for the Fleet edge firmware.
  ******************************************************************************
  * @attention
  *
  * The compensation routines below are the Bosch reference fixed-point
  * implementations transcribed from the BME280 datasheet (rev 1.6) appendix.
  * They are NOT re-derived here: each BME280 is factory-trimmed and the only
  * correct way to turn its raw ADC counts into engineering units is to run them
  * through this exact math with the chip's own NVM coefficients.
  ******************************************************************************
  */
#include "bme280.h"

/* --- Register map (datasheet section 5.3) --------------------------------- */
#define BME280_REG_CALIB00   0x88U   /* dig_T1..dig_P9, dig_H1: 26 bytes 0x88..0xA1 */
#define BME280_REG_CALIB26   0xE1U   /* dig_H2..dig_H6:          7 bytes 0xE1..0xE7 */
#define BME280_REG_CTRL_HUM  0xF2U   /* humidity oversampling  (write before ctrl_meas) */
#define BME280_REG_CTRL_MEAS 0xF4U   /* temp/press oversampling + power mode */
#define BME280_REG_CONFIG    0xF5U   /* standby time, IIR filter */
#define BME280_REG_DATA      0xF7U   /* press(3) temp(3) hum(2): 8-byte burst */

/* --- Config values -------------------------------------------------------- */
#define BME280_OSRS_X1       0x01U   /* oversampling x1 for each channel */
#define BME280_MODE_NORMAL   0x03U   /* continuous measure (vs sleep/forced) */
/* ctrl_meas = osrs_t[7:5] | osrs_p[4:2] | mode[1:0] */
#define BME280_CTRL_MEAS_VAL ((BME280_OSRS_X1 << 5) | (BME280_OSRS_X1 << 2) | BME280_MODE_NORMAL)
/* ctrl_hum = osrs_h[2:0] */
#define BME280_CTRL_HUM_VAL  (BME280_OSRS_X1)
/* config: standby 0.5ms, filter off - fine for a 10 Hz telemetry sample rate. */
#define BME280_CONFIG_VAL    0x00U

#define BME280_TIMEOUT       100U    /* ms per HAL transaction */

/* -------------------------------------------------------------------------- */
/*  Bosch datasheet compensation (verbatim reference, do not "simplify")       */
/* -------------------------------------------------------------------------- */

/* Returns temperature in 0.01 degC (e.g. 5123 -> 51.23 C). Sets t_fine. */
static int32_t compensate_temperature(BME280_Calib_t *c, int32_t adc_T)
{
  int32_t var1, var2, T;
  var1 = ((((adc_T >> 3) - ((int32_t)c->dig_T1 << 1))) * ((int32_t)c->dig_T2)) >> 11;
  var2 = (((((adc_T >> 4) - ((int32_t)c->dig_T1)) *
            ((adc_T >> 4) - ((int32_t)c->dig_T1))) >> 12) *
          ((int32_t)c->dig_T3)) >> 14;
  c->t_fine = var1 + var2;
  T = (c->t_fine * 5 + 128) >> 8;
  return T;
}

/* Returns pressure in Q24.8 Pa (Pa = value / 256). Depends on t_fine. */
static uint32_t compensate_pressure(BME280_Calib_t *c, int32_t adc_P)
{
  int64_t var1, var2, p;
  var1 = ((int64_t)c->t_fine) - 128000;
  var2 = var1 * var1 * (int64_t)c->dig_P6;
  var2 = var2 + ((var1 * (int64_t)c->dig_P5) << 17);
  var2 = var2 + (((int64_t)c->dig_P4) << 35);
  var1 = ((var1 * var1 * (int64_t)c->dig_P3) >> 8) +
         ((var1 * (int64_t)c->dig_P2) << 12);
  var1 = (((((int64_t)1) << 47) + var1)) * ((int64_t)c->dig_P1) >> 33;
  if (var1 == 0)
  {
    return 0; /* avoid divide-by-zero */
  }
  p = 1048576 - adc_P;
  p = (((p << 31) - var2) * 3125) / var1;
  var1 = (((int64_t)c->dig_P9) * (p >> 13) * (p >> 13)) >> 25;
  var2 = (((int64_t)c->dig_P8) * p) >> 19;
  p = ((p + var1 + var2) >> 8) + (((int64_t)c->dig_P7) << 4);
  return (uint32_t)p;
}

/* Returns humidity in Q22.10 %RH (%RH = value / 1024). Depends on t_fine. */
static uint32_t compensate_humidity(BME280_Calib_t *c, int32_t adc_H)
{
  int32_t v_x1_u32r;
  v_x1_u32r = (c->t_fine - ((int32_t)76800));
  v_x1_u32r = (((((adc_H << 14) - (((int32_t)c->dig_H4) << 20) -
                  (((int32_t)c->dig_H5) * v_x1_u32r)) + ((int32_t)16384)) >> 15) *
               (((((((v_x1_u32r * ((int32_t)c->dig_H6)) >> 10) *
                    (((v_x1_u32r * ((int32_t)c->dig_H3)) >> 11) + ((int32_t)32768))) >> 10) +
                  ((int32_t)2097152)) * ((int32_t)c->dig_H2) + 8192) >> 14));
  v_x1_u32r = (v_x1_u32r - (((((v_x1_u32r >> 15) * (v_x1_u32r >> 15)) >> 7) *
                             ((int32_t)c->dig_H1)) >> 4));
  v_x1_u32r = (v_x1_u32r < 0 ? 0 : v_x1_u32r);
  v_x1_u32r = (v_x1_u32r > 419430400 ? 419430400 : v_x1_u32r);
  return (uint32_t)(v_x1_u32r >> 12);
}

/* -------------------------------------------------------------------------- */

/* Read the two NVM calibration blocks and unpack them per the datasheet's
   little-endian layout. dig_H4/H5 are the awkward 12-bit split across E4/E5/E6. */
static HAL_StatusTypeDef read_calibration(BME280_t *dev)
{
  uint8_t b[26];   /* 0x88..0xA1 */
  uint8_t h[7];    /* 0xE1..0xE7 */
  HAL_StatusTypeDef st;

  st = HAL_I2C_Mem_Read(dev->hi2c, dev->addr, BME280_REG_CALIB00,
                        I2C_MEMADD_SIZE_8BIT, b, sizeof(b), BME280_TIMEOUT);
  if (st != HAL_OK) { return st; }

  st = HAL_I2C_Mem_Read(dev->hi2c, dev->addr, BME280_REG_CALIB26,
                        I2C_MEMADD_SIZE_8BIT, h, sizeof(h), BME280_TIMEOUT);
  if (st != HAL_OK) { return st; }

  BME280_Calib_t *c = &dev->calib;
  c->dig_T1 = (uint16_t)(b[0]  | (b[1]  << 8));
  c->dig_T2 = (int16_t) (b[2]  | (b[3]  << 8));
  c->dig_T3 = (int16_t) (b[4]  | (b[5]  << 8));
  c->dig_P1 = (uint16_t)(b[6]  | (b[7]  << 8));
  c->dig_P2 = (int16_t) (b[8]  | (b[9]  << 8));
  c->dig_P3 = (int16_t) (b[10] | (b[11] << 8));
  c->dig_P4 = (int16_t) (b[12] | (b[13] << 8));
  c->dig_P5 = (int16_t) (b[14] | (b[15] << 8));
  c->dig_P6 = (int16_t) (b[16] | (b[17] << 8));
  c->dig_P7 = (int16_t) (b[18] | (b[19] << 8));
  c->dig_P8 = (int16_t) (b[20] | (b[21] << 8));
  c->dig_P9 = (int16_t) (b[22] | (b[23] << 8));
  /* b[24] (0xA0) reserved */
  c->dig_H1 = b[25];

  c->dig_H2 = (int16_t)(h[0] | (h[1] << 8));
  c->dig_H3 = h[2];
  c->dig_H4 = (int16_t)(((int8_t)h[3] << 4) | (h[4] & 0x0F));
  c->dig_H5 = (int16_t)(((int8_t)h[5] << 4) | (h[4] >> 4));
  c->dig_H6 = (int8_t)h[6];

  return HAL_OK;
}

HAL_StatusTypeDef BME280_Init(BME280_t *dev, I2C_HandleTypeDef *hi2c, uint16_t addr)
{
  HAL_StatusTypeDef st;
  uint8_t v;

  dev->hi2c = hi2c;
  dev->addr = addr;

  st = read_calibration(dev);
  if (st != HAL_OK) { return st; }

  /* ctrl_hum MUST be written before ctrl_meas: the humidity oversampling only
     latches when ctrl_meas is written afterwards (datasheet 5.4.3). */
  v = BME280_CTRL_HUM_VAL;
  st = HAL_I2C_Mem_Write(dev->hi2c, dev->addr, BME280_REG_CTRL_HUM,
                         I2C_MEMADD_SIZE_8BIT, &v, 1, BME280_TIMEOUT);
  if (st != HAL_OK) { return st; }

  v = BME280_CONFIG_VAL;
  st = HAL_I2C_Mem_Write(dev->hi2c, dev->addr, BME280_REG_CONFIG,
                         I2C_MEMADD_SIZE_8BIT, &v, 1, BME280_TIMEOUT);
  if (st != HAL_OK) { return st; }

  v = BME280_CTRL_MEAS_VAL;
  st = HAL_I2C_Mem_Write(dev->hi2c, dev->addr, BME280_REG_CTRL_MEAS,
                         I2C_MEMADD_SIZE_8BIT, &v, 1, BME280_TIMEOUT);
  return st;
}

HAL_StatusTypeDef BME280_Read(BME280_t *dev,
                              float *temperature_c,
                              float *pressure_hpa,
                              float *humidity_pct)
{
  uint8_t d[8];
  HAL_StatusTypeDef st;
  int32_t adc_P, adc_T, adc_H;

  /* One 8-byte burst so pressure/temp/humidity come from the same conversion. */
  st = HAL_I2C_Mem_Read(dev->hi2c, dev->addr, BME280_REG_DATA,
                        I2C_MEMADD_SIZE_8BIT, d, sizeof(d), BME280_TIMEOUT);
  if (st != HAL_OK) { return st; }

  /* Pressure & temperature are 20-bit; humidity is 16-bit. */
  adc_P = ((int32_t)d[0] << 12) | ((int32_t)d[1] << 4) | (d[2] >> 4);
  adc_T = ((int32_t)d[3] << 12) | ((int32_t)d[4] << 4) | (d[5] >> 4);
  adc_H = ((int32_t)d[6] << 8)  |  (int32_t)d[7];

  /* Temperature first: it sets t_fine, which pressure and humidity both need. */
  *temperature_c = compensate_temperature(&dev->calib, adc_T) / 100.0f;
  *pressure_hpa  = compensate_pressure(&dev->calib, adc_P) / 25600.0f; /* Q24.8 Pa -> hPa */
  *humidity_pct  = compensate_humidity(&dev->calib, adc_H) / 1024.0f;

  return HAL_OK;
}
